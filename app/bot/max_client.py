from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
from pathlib import Path
from typing import Mapping
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger(__name__)


class MaxApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, response: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class MaxClient:
    """Small async REST client for MAX Bot API.

    MAX requires the bot token in the Authorization header, not in query params.
    """

    def __init__(
        self,
        *,
        token: str,
        base_url: str = "https://platform-api.max.ru",
        timeout: float = 35.0,
        trust_env: bool = False,
    ) -> None:
        if not token:
            raise RuntimeError("MAX_BOT_TOKEN is empty")
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=15.0),
            headers={"Authorization": token},
            trust_env=trust_env,
        )
        self.upload_http = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=20.0),
            trust_env=trust_env,
        )

    async def close(self) -> None:
        await self.http.aclose()
        await self.upload_http.aclose()

    async def get_me(self) -> dict[str, Any]:
        return await self._request_json("GET", "/me")

    async def get_updates(
        self,
        *,
        marker: int | None = None,
        timeout: int = 30,
        limit: int = 100,
        types: list[str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"timeout": timeout, "limit": limit}
        if marker is not None:
            params["marker"] = marker
        if types:
            # The docs show comma-separated values, e.g. message_created,message_callback.
            params["types"] = ",".join(types)
        return await self._request_json("GET", "/updates", params=params)

    async def get_message(self, message_id: str | int) -> dict[str, Any]:
        return await self._request_json("GET", f"/messages/{_int_or_str(message_id)}")

    async def get_messages(
        self,
        *,
        chat_id: str | int | None = None,
        message_ids: list[str | int] | str | int | None = None,
        count: int = 10,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> dict[str, Any]:
        """Return recent messages from chat or specific messages.

        MAX voice notes can arrive as compact update events. In that case the
        safest fallback is to read the latest chat messages and inspect the real
        Message.body.attachments object.
        """
        params: dict[str, Any] = {"count": max(1, min(int(count or 10), 100))}
        if chat_id is not None:
            params["chat_id"] = _int_or_str(chat_id)
        elif message_ids is not None:
            if isinstance(message_ids, (list, tuple, set)):
                params["message_ids"] = ",".join(str(item) for item in message_ids)
            else:
                params["message_ids"] = str(message_ids)
        else:
            raise ValueError("chat_id or message_ids is required for MAX get_messages")
        if from_ts is not None:
            params["from"] = int(from_ts)
        if to_ts is not None:
            params["to"] = int(to_ts)
        return await self._request_json("GET", "/messages", params=params)

    async def download_url_to_file(self, url: str, path: str | Path, *, max_bytes: int | None = None) -> Path:
        """Download an attachment URL returned by MAX into a local file."""
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        headers_options: list[Mapping[str, str] | None] = [None, {"Authorization": self.token}]
        last_error: Exception | None = None
        for headers in headers_options:
            try:
                async with self.upload_http.stream("GET", url, headers=headers) as response:
                    if response.status_code >= 400:
                        last_error = MaxApiError(
                            f"MAX attachment download failed: HTTP {response.status_code}",
                            status_code=response.status_code,
                            response=_safe_response(response),
                        )
                        continue
                    total = 0
                    with dest.open("wb") as fh:
                        async for chunk in response.aiter_bytes():
                            if not chunk:
                                continue
                            total += len(chunk)
                            if max_bytes and total > max_bytes:
                                raise MaxApiError("MAX attachment is too large", response={"max_bytes": max_bytes})
                            fh.write(chunk)
                    return dest
            except Exception as exc:  # try the second authorization mode before failing
                last_error = exc
                continue
        if last_error:
            raise last_error
        raise MaxApiError("MAX attachment download failed")

    async def send_message(
        self,
        *,
        chat_id: str | int | None = None,
        user_id: str | int | None = None,
        text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        text_format: str | None = None,
        notify: bool = True,
        disable_link_preview: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"disable_link_preview": disable_link_preview}
        if chat_id:
            params["chat_id"] = _int_or_str(chat_id)
        elif user_id:
            params["user_id"] = _int_or_str(user_id)
        else:
            raise ValueError("chat_id or user_id is required for MAX send_message")

        payload: dict[str, Any] = {
            "text": text,
            "notify": notify,
        }
        if attachments is not None:
            payload["attachments"] = attachments
        if text_format in {"markdown", "html"}:
            payload["format"] = text_format
        return await self._request_json("POST", "/messages", params=params, json=payload)

    async def send_chat_action(self, chat_id: str | int, action: str = "typing_on") -> None:
        try:
            await self._request_json("POST", f"/chats/{_int_or_str(chat_id)}/actions", json={"action": action})
        except MaxApiError as exc:
            # Typing is nice-to-have. Do not break the dialog if MAX does not accept it in a private chat.
            logger.debug("MAX typing action failed chat_id=%s status=%s response=%s", chat_id, exc.status_code, exc.response)

    async def upload_file(self, path: str | Path, *, upload_type: str = "image") -> dict[str, Any]:
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(str(file_path))
        if upload_type == "photo":
            upload_type = "image"

        init_payload = await self._request_json("POST", "/uploads", params={"type": upload_type})
        upload_url = init_payload.get("url")
        if not upload_url:
            raise MaxApiError("MAX /uploads response does not contain url", response=init_payload)

        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        with file_path.open("rb") as file_obj:
            response = await self.upload_http.post(
                upload_url,
                files={"data": (file_path.name, file_obj, mime_type)},
            )
        if response.status_code >= 400:
            raise MaxApiError(
                f"MAX media upload failed: HTTP {response.status_code}",
                status_code=response.status_code,
                response=_safe_response(response),
            )
        try:
            upload_payload = response.json()
        except ValueError:
            upload_payload = {"raw": response.text}

        # MAX behaves differently for different media types. For images the upload host
        # can return an empty/non-JSON body while the usable media token is already
        # embedded in the upload URL as `photoIds`. Older examples/documentation call
        # this value just `token`, so normalize all known shapes to {"token": ...}.
        token = (
            _find_media_token(upload_payload)
            or _find_media_token(init_payload)
            or _token_from_url(upload_url, upload_type=upload_type)
        )
        if token and "token" not in upload_payload:
            upload_payload["token"] = token
        if not upload_payload.get("token"):
            raise MaxApiError("MAX upload response does not contain media token", response=upload_payload)
        return upload_payload

    async def send_image(
        self,
        *,
        chat_id: str | int | None = None,
        user_id: str | int | None = None,
        path: str | Path,
        caption: str | None = None,
        text_format: str | None = None,
    ) -> dict[str, Any]:
        payload = await self.upload_file(path, upload_type="image")
        attachment = {"type": "image", "payload": payload}
        # MAX can need a short delay before the media is ready.
        delays = [0.0, 1.5, 3.0, 6.0]
        last_exc: Exception | None = None
        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                return await self.send_message(
                    chat_id=chat_id,
                    user_id=user_id,
                    text=caption or "",
                    attachments=[attachment],
                    text_format=text_format,
                )
            except MaxApiError as exc:
                last_exc = exc
                response_text = str(exc.response or "")
                if exc.status_code == 400 and "attachment.not.ready" in response_text:
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    async def send_images(
        self,
        *,
        chat_id: str | int | None = None,
        user_id: str | int | None = None,
        paths: list[str | Path],
        caption: str | None = None,
        text_format: str | None = None,
    ) -> dict[str, Any]:
        """Send several images as one MAX message with multiple attachments.

        MAX does not have a Telegram-style sendMediaGroup method. Its equivalent is
        one POST /messages call with an attachments array. MAX docs explicitly define
        `attachments` as a list, so a batch of image attachments is the closest
        album/media-group behaviour supported by the platform.
        """
        normalized_paths = [Path(path) for path in (paths or [])]
        if not normalized_paths:
            return await self.send_message(
                chat_id=chat_id,
                user_id=user_id,
                text=caption or "",
                text_format=text_format,
            )

        attachments: list[dict[str, Any]] = []
        for path in normalized_paths:
            payload = await self.upload_file(path, upload_type="image")
            attachments.append({"type": "image", "payload": payload})

        # MAX may need a short delay before newly uploaded media are processed.
        delays = [0.0, 1.5, 3.0, 6.0]
        last_exc: Exception | None = None
        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                return await self.send_message(
                    chat_id=chat_id,
                    user_id=user_id,
                    text=caption or "",
                    attachments=attachments,
                    text_format=text_format,
                )
            except MaxApiError as exc:
                last_exc = exc
                response_text = str(exc.response or "")
                if exc.status_code == 400 and "attachment.not.ready" in response_text:
                    continue
                raise
        assert last_exc is not None
        raise last_exc


    async def subscribe_webhook(self, *, url: str, secret: str | None = None, update_types: list[str] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"url": url}
        if update_types:
            payload["update_types"] = update_types
        if secret:
            payload["secret"] = secret
        return await self._request_json("POST", "/subscriptions", json=payload)

    async def _request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self.http.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise MaxApiError(
                f"MAX API request failed: {method} {path} HTTP {response.status_code}",
                status_code=response.status_code,
                response=_safe_response(response),
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise MaxApiError(f"MAX API returned non-JSON response: {response.text[:500]}") from exc


def _safe_response(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:1000]


def _int_or_str(value: str | int) -> int | str:
    if isinstance(value, int):
        return value
    value_str = str(value).strip()
    return int(value_str) if re.fullmatch(r"-?\d+", value_str) else value_str


def _find_media_token(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in (
            "token",
            "photoIds",
            "photo_ids",
            "photoId",
            "photo_id",
            "file_token",
            "fileToken",
            "media_token",
            "mediaToken",
        ):
            raw = value.get(key)
            if isinstance(raw, str) and raw:
                return raw
            if isinstance(raw, list) and raw and isinstance(raw[0], str):
                return raw[0]
        for nested in value.values():
            found = _find_media_token(nested)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_media_token(item)
            if found:
                return found
    return None


def _token_from_url(url: str, *, upload_type: str | None = None) -> str | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    # For image upload URLs MAX currently uses `photoIds` as the attachment token.
    # `apiToken` is only for the upload endpoint itself and must not be used as the
    # message attachment token unless MAX changes the API and no better key exists.
    preferred_keys = ["token", "file_token", "t", "photoIds", "photoId", "photo_ids", "photo_id"]
    if upload_type == "image":
        preferred_keys = ["photoIds", "photoId", "photo_ids", "photo_id", "token", "file_token", "t"]

    for key in preferred_keys:
        values = query.get(key)
        if values and values[0]:
            return values[0]
    return None
