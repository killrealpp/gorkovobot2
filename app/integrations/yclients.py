from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.core.config import get_settings


class YClientsError(RuntimeError):
    pass


logger = logging.getLogger(__name__)


class YClientsClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.yclients_base_url.rstrip("/")
        self.company_id = settings.yclients_company_id
        self.partner_token = settings.yclients_partner_token
        self.user_token = settings.yclients_user_token
        if not self.company_id or not self.partner_token or not self.user_token:
            raise YClientsError("YCLIENTS credentials are not configured")

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.yclients.v2+json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.partner_token}, User {self.user_token}",
        }

    def get_book_times(self, *, staff_id: str, service_id: str | None = None, date: str) -> list[Any]:
        logger.debug("YCLIENTS get_book_times staff_id=%s service_id=%s date=%s", staff_id, service_id, date)
        params = {}
        if service_id:
            params["service_ids[]"] = service_id

        data = self._request(
            "GET",
            f"/book_times/{self.company_id}/{staff_id}/{date}",
            params=params,
            quiet=True,
        )
        times = self._as_list(data)
        logger.debug("YCLIENTS get_book_times result staff_id=%s service_id=%s date=%s count=%s", staff_id, service_id, date, len(times))
        return times

    def get_records(self, *, staff_id: str | None = None, date: str | None = None, start_date: str | None = None, end_date: str | None = None, page: int = 1) -> list[Any]:
        params: dict[str, str] = {"page": str(page)}
        if staff_id:
            params["staff_id"] = staff_id
        if date:
            params["date"] = date
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date

        data = self._request(
            "GET",
            f"/records/{self.company_id}",
            params=params,
        )
        return self._as_list(data)

    def create_book_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        appointments = payload.get("appointments") or []
        logger.info("YCLIENTS create_book_record appointments=%s phone_present=%s", len(appointments), bool(payload.get("phone")))
        return self._request("POST", f"/book_record/{self.company_id}", json=payload)

    def update_record(self, record_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        logger.info("YCLIENTS update_record record_id=%s", record_id)
        return self._request("PUT", f"/record/{self.company_id}/{record_id}", json=payload)

    def delete_record(self, record_id: str) -> dict[str, Any]:
        logger.info("YCLIENTS delete_record record_id=%s", record_id)
        return self._request("DELETE", f"/record/{self.company_id}/{record_id}")

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        quiet = bool(kwargs.pop("quiet", False))
        last_error = None
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                log = logger.debug if quiet else logger.info
                log("YCLIENTS request start method=%s path=%s attempt=%s", method, path, attempt + 1)
                with httpx.Client(timeout=20) as client:
                    response = client.request(method, f"{self.base_url}{path}", headers=self.headers, **kwargs)
                log("YCLIENTS request response method=%s path=%s status=%s", method, path, response.status_code)

                # DELETE in YClients commonly returns 204 No Content on success.
                # Treat it as success and do not try to parse an empty body as JSON.
                if method.upper() == "DELETE" and response.status_code in {200, 202, 204}:
                    if not response.text.strip():
                        return {}

                # Idempotent delete: if the record is already gone, the booking is
                # effectively canceled in YClients. Do not retry and turn success
                # into a fake failure after a previous 204.
                if method.upper() == "DELETE" and response.status_code == 404:
                    logger.info("YCLIENTS delete_record already_absent path=%s", path)
                    return {}

                if response.status_code >= 400:
                    raise YClientsError(f"YCLIENTS error {response.status_code}: {response.text}")

                if not response.text.strip():
                    return {}

                payload = response.json()
                if isinstance(payload, dict) and payload.get("success") is False:
                    raise YClientsError(str(payload.get("meta") or payload.get("message") or payload))
                return payload.get("data", payload) if isinstance(payload, dict) else payload
            except Exception as exc:
                last_error = exc
                logger.warning("YCLIENTS request failed method=%s path=%s attempt=%s error=%s", method, path, attempt + 1, exc)
                if attempt < 2:
                    time.sleep(0.7 * (attempt + 1))
        raise YClientsError(f"YCLIENTS request failed: {last_error}")

    @staticmethod
    def _as_list(data: Any) -> list[Any]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("items", "services", "staff", "times"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        return []
