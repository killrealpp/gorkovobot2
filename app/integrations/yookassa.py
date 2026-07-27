from __future__ import annotations

from decimal import Decimal
import logging
import re
import time
from typing import Any
from uuid import uuid4

import httpx

from app.core.config import get_settings


class YooKassaError(RuntimeError):
    pass


logger = logging.getLogger(__name__)


class YooKassaClient:
    BASE_URL = "https://api.yookassa.ru/v3"

    def __init__(self) -> None:
        settings = get_settings()
        self.shop_id = settings.payment_shop_id
        self.secret_key = settings.payment_secret_key
        self.return_url = settings.payment_success_url or "https://t.me/"
        if not self.shop_id or not self.secret_key:
            raise YooKassaError("YooKassa credentials are not configured")

    def create_payment(
        self,
        *,
        amount: Decimal,
        description: str,
        metadata: dict[str, Any],
        customer_phone: str | None = None,
    ) -> dict[str, Any]:
        logger.info("YooKassa create_payment amount=%s customer_phone_present=%s", amount, bool(customer_phone))
        payload: dict[str, Any] = {
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": self.return_url},
            "description": description[:128],
            "metadata": metadata,
        }
        if customer_phone:
            digits = normalize_phone(customer_phone)
            payload["receipt"] = {
                "customer": {"phone": digits},
                "items": [
                    {
                        "description": description[:128],
                        "quantity": "1.00",
                        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                        "vat_code": 1,
                        "payment_subject": "service",
                        "payment_mode": "partial_prepayment",
                    }
                ],
            }
        response = self._request("POST", "/payments", json=payload, headers={"Idempotence-Key": str(uuid4())})
        logger.info("YooKassa create_payment result payment_id=%s status=%s", response.get("id"), response.get("status"))
        return response

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        logger.info("YooKassa get_payment payment_id=%s", payment_id)
        response = self._request("GET", f"/payments/{payment_id}")
        logger.info("YooKassa get_payment result payment_id=%s status=%s paid=%s", payment_id, response.get("status"), response.get("paid"))
        return response

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                logger.info("YooKassa request start method=%s path=%s attempt=%s", method, path, attempt + 1)
                with httpx.Client(timeout=20, auth=(self.shop_id, self.secret_key)) as client:
                    response = client.request(method, f"{self.BASE_URL}{path}", **kwargs)
                logger.info("YooKassa request response method=%s path=%s status=%s", method, path, response.status_code)
                if response.status_code >= 400:
                    raise YooKassaError(f"YooKassa error {response.status_code}: {response.text}")
                return response.json()
            except Exception as exc:
                last_error = exc
                logger.warning("YooKassa request failed method=%s path=%s attempt=%s error=%s", method, path, attempt + 1, exc)
                if isinstance(exc, YooKassaError) and ("401" in str(exc) or "403" in str(exc) or "invalid_credentials" in str(exc)):
                    break
                if attempt < 2:
                    time.sleep(0.7 * (attempt + 1))
        raise YooKassaError(f"YooKassa request failed: {last_error}")


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("9") and len(digits) == 10:
        digits = "7" + digits
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if not (digits.startswith("7") and len(digits) == 11):
        raise YooKassaError("Invalid phone")
    return digits
