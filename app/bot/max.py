from __future__ import annotations

import asyncio
import json
import logging
import re
import signal
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, time
from typing import Any

from app.ai.audio import VoiceTranscriptionError, transcribe_audio_file
from app.ai.rate_limit import is_llm_rate_limited
from app.ai.parser import render_watchlist_triggered_message
from app.bot.max_client import MaxApiError, MaxClient
from app.bot.media import extract_media_titles_from_reply, paths_for_requested_media
from app.core.config import PROJECT_ROOT, get_settings
from app.dialog.availability_cache import refresh_availability_cache
from app.dialog.engine import handle_text, pop_requested_media
from app.dialog.payment_status import sync_paid_bookings
from app.dialog.watchlist import check_active_watchlist
from app.maintenance.scheduler import retention_cleanup_loop
from app.storage import sqlite

logger = logging.getLogger(__name__)
_chat_locks: dict[str, asyncio.Lock] = {}
EMPTY_REPLY_FALLBACK = "Подскажите, пожалуйста, что хотите уточнить?"


@dataclass(frozen=True)
class MaxTarget:
    chat_id: str | None = None
    user_id: str | None = None

    @property
    def storage_chat_id(self) -> str:
        return str(self.chat_id or self.user_id or "")


def _chat_lock(chat_id: str) -> asyncio.Lock:
    lock = _chat_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_locks[chat_id] = lock
    return lock


async def run_bot() -> None:
    settings = get_settings()
    client = MaxClient(
        token=settings.max_bot_token,
        base_url=settings.max_api_base_url,
        trust_env=bool(settings.http_trust_env),
    )
    tasks: list[asyncio.Task[Any]] = []
    try:
        me = await client.get_me()
        logger.info("MAX bot started name=%s username=%s user_id=%s", me.get("name"), me.get("username"), me.get("user_id"))
        tasks.append(asyncio.create_task(_payment_status_loop(client)))
        tasks.append(asyncio.create_task(_admin_notification_loop(client)))
        tasks.append(asyncio.create_task(_availability_cache_loop()))
        tasks.append(asyncio.create_task(_watchlist_loop(client)))
        if settings.retention_cleanup_enabled:
            tasks.append(asyncio.create_task(retention_cleanup_loop()))
        if settings.startup_availability_refresh_enabled:
            tasks.append(asyncio.create_task(_startup_availability_cache_refresh()))

        if settings.max_webhook_enabled or settings.max_mode.lower() == "webhook":
            await _run_webhook(client)
        else:
            await _run_polling(client)
    finally:
        for task in tasks:
            task.cancel()
        await client.close()


async def _run_polling(client: MaxClient) -> None:
    settings = get_settings()
    marker: int | None = None
    stop = asyncio.Event()

    def _stop(*_: object) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    # Voice messages in MAX can arrive with a slightly different payload shape.
    # Do not over-filter polling events when voice support is enabled: we still
    # ignore unrelated events in _map_update, but this lets us see voice events.
    update_types = None if settings.voice_messages_enabled else ["message_created", "bot_started"]
    logger.info("MAX polling started update_types=%s", update_types or "all")
    while not stop.is_set():
        try:
            data = await client.get_updates(marker=marker, timeout=30, limit=100, types=update_types)
            marker_value = data.get("marker")
            if marker_value is not None:
                marker = int(marker_value)
            updates = data.get("updates") or []
            if updates:
                logger.info(
                    "MAX_UPDATES_RECEIVED count=%s marker=%s summaries=%s",
                    len(updates),
                    marker,
                    [_safe_update_summary(item) for item in updates[:5]],
                )
            for update in updates:
                await _process_update(client, update)
        except MaxApiError as exc:
            if exc.status_code == 429:
                logger.warning("MAX polling 429, sleeping %s seconds", settings.max_poll_429_sleep_seconds)
                await asyncio.sleep(settings.max_poll_429_sleep_seconds)
            else:
                logger.exception("MAX polling API error status=%s response=%s", exc.status_code, exc.response)
                await asyncio.sleep(settings.max_poll_error_sleep_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MAX polling failed")
            await asyncio.sleep(settings.max_poll_error_sleep_seconds)


async def _run_webhook(client: MaxClient) -> None:
    settings = get_settings()
    try:
        import uvicorn
        from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
    except ImportError as exc:
        raise RuntimeError("Webhook mode requires fastapi and uvicorn. Install requirements.txt") from exc

    app = FastAPI(title="MAX booking bot")
    from app.api.public_catalog import register_public_catalog_routes

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    register_public_catalog_routes(app)

    @app.post(settings.max_webhook_path)
    async def webhook(
        update: dict[str, Any],
        background_tasks: BackgroundTasks,
        x_max_bot_api_secret: str | None = Header(default=None),
    ) -> dict[str, bool]:
        if settings.max_webhook_secret and x_max_bot_api_secret != settings.max_webhook_secret:
            raise HTTPException(status_code=403, detail="invalid MAX webhook secret")
        background_tasks.add_task(_process_update, client, update)
        return {"ok": True}

    if settings.max_webhook_url:
        try:
            subscribed = await client.subscribe_webhook(
                url=settings.max_webhook_url,
                secret=settings.max_webhook_secret or None,
                update_types=None if settings.voice_messages_enabled else ["message_created", "bot_started"],
            )
            logger.info("MAX webhook subscription response=%s", subscribed)
        except Exception:
            logger.exception("MAX webhook subscription failed. Server still starts; check MAX_WEBHOOK_URL/SECRET")

    config = uvicorn.Config(app, host=settings.max_webhook_host, port=settings.max_webhook_port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()



async def _process_update(client: MaxClient, update: dict[str, Any]) -> None:
    inbound = await _map_update(client, update)
    if inbound is None:
        logger.info("MAX_UPDATE_IGNORED summary=%s", _safe_update_summary(update))
        return
    target, text, user_name, raw_message_id, event_type = inbound
    chat_id = target.storage_chat_id
    if not chat_id:
        logger.warning("MAX update ignored because target is empty update=%s", update)
        return

    async with _chat_lock(chat_id):
        if text.startswith("/admin") or text.lower().strip() in {"админ", "admin", "мой id", "мой айди"}:
            await _handle_admin_bind_command(client, target, text)
            return

        if event_type == "bot_started" or text == "/start":
            sqlite.clear_messages(chat_id)
            sqlite.release_holds(chat_id)

            from app.dialog.state import BookingDraft

            draft = BookingDraft()
            sqlite.save_draft(chat_id, draft.to_dict(), status="waiting_user", current_step=draft.next_step())
            sqlite.add_message(chat_id, "user", "/start", raw={"max_message_id": raw_message_id, "update": update})
            reply = _ensure_non_empty_reply(await _safe_handle(chat_id, user_name, "/start"), chat_id=chat_id, text="/start")
            if reply:
                sqlite.add_message(chat_id, "assistant", reply)
                await _send_text(client, target, reply)
            return

        typing_task = asyncio.create_task(_typing_loop(client, target))
        requested_media: list[str] = []
        reply = ""
        try:
            sqlite.add_message(chat_id, "user", text, raw={"max_message_id": raw_message_id, "update": update})
            reply = _ensure_non_empty_reply(await _safe_handle(chat_id, user_name, text), chat_id=chat_id, text=text)
            requested_media = pop_requested_media(chat_id)
            marker_media = extract_media_titles_from_reply(reply)
            if marker_media:
                requested_media = marker_media
            logger.info(
                "MAX_MEDIA_DECISION chat_id=%s requested_media=%s marker_media=%s",
                chat_id,
                requested_media,
                marker_media,
            )
            sqlite.add_message(chat_id, "assistant", reply)
        finally:
            typing_task.cancel()

    if reply:
        await _send_text(client, target, reply)
    await _send_requested_media(client, target, requested_media)


def _ensure_non_empty_reply(reply: str, *, chat_id: str, text: str) -> str:
    clean = (reply or "").strip()
    if clean:
        return clean
    logger.warning("EMPTY_DIALOG_REPLY chat_id=%s text=%r", chat_id, text)
    return EMPTY_REPLY_FALLBACK


async def _map_update(client: MaxClient, update: dict[str, Any]) -> tuple[MaxTarget, str, str, str | None, str] | None:
    update_type = str(update.get("update_type") or "")
    if update_type in {"bot_stopped", "bot_removed", "dialog_removed", "dialog_cleared"}:
        return None

    if update_type == "bot_started":
        user = update.get("user") or {}
        return (
            MaxTarget(chat_id=_string_or_none(update.get("chat_id")), user_id=_string_or_none(user.get("user_id"))),
            "/start",
            _user_name(user),
            None,
            "bot_started",
        )

    settings = get_settings()
    if update_type != "message_created":
        # Some MAX clients deliver mobile voice notes as a compact non-message update.
        # The update can only contain chat_id + marker/time, while the real attachment
        # is visible through GET /messages?chat_id=... . Do not drop it silently.
        if settings.voice_messages_enabled:
            fallback = await _voice_from_recent_chat_messages(client, update)
            if fallback is not None:
                return fallback
        # If the event contains a Message-like object, try to parse it below.
        if not (settings.voice_messages_enabled and isinstance(update, dict) and ("message" in update or "body" in update)):
            logger.info("MAX_NON_MESSAGE_UPDATE_IGNORED summary=%s", _safe_update_summary(update))
            return None
        logger.info("MAX_MESSAGE_LIKE_UPDATE update_type=%s keys=%s", update_type, sorted(update.keys()))

    message = update.get("message") or update.get("body") or {}
    sender = message.get("sender") or update.get("user") or {}
    if bool(sender.get("is_bot")):
        return None

    body = message.get("body") or {}
    recipient = message.get("recipient") or {}
    chat_id = (
        _string_or_none(update.get("chat_id"))
        or _string_or_none(recipient.get("chat_id"))
        or _string_or_none(message.get("chat_id"))
    )
    user_id = _string_or_none(sender.get("user_id")) or _string_or_none(update.get("user_id"))
    raw_message_id = _string_or_none(message.get("mid") or message.get("message_id") or message.get("id") or update.get("mid") or update.get("message_id") or update.get("id"))
    target = MaxTarget(chat_id=chat_id, user_id=user_id)

    text = _extract_text_from_message_body(body)
    if text:
        return (target, text, _user_name(sender), raw_message_id, "message_created")

    if not settings.voice_messages_enabled:
        logger.info(
            "MAX_EMPTY_MESSAGE_IGNORED voice_enabled=false update_type=%s message_keys=%s body_keys=%s raw_message_id=%s",
            update_type,
            sorted(message.keys()) if isinstance(message, dict) else type(message).__name__,
            sorted(body.keys()) if isinstance(body, dict) else type(body).__name__,
            raw_message_id,
        )
        return None

    voice_attachment = _find_voice_attachment(message) or _find_voice_attachment(update)

    # In long polling MAX can send a compact message event first: body/text is
    # empty and the attachment metadata is only available via GET /messages/{id}.
    # Previously we fetched the full message only after a voice attachment had
    # already been found, so such voice notes were ignored and the typing action
    # never started.
    if not voice_attachment and raw_message_id:
        try:
            logger.info("MAX_VOICE_TRY_GET_MESSAGE message_id=%s update_type=%s", raw_message_id, update_type)
            full_message = await client.get_message(raw_message_id)
            voice_attachment = _find_voice_attachment(full_message)
            if voice_attachment:
                logger.info("MAX_VOICE_DETECTED_FROM_FULL_MESSAGE message_id=%s", raw_message_id)
        except Exception:
            logger.debug("MAX get_message before voice detection failed message_id=%s", raw_message_id, exc_info=True)

    if not voice_attachment:
        logger.info(
            "MAX_MESSAGE_WITHOUT_TEXT_OR_VOICE update_type=%s message_keys=%s body_keys=%s raw_message_id=%s attachment_summary=%s",
            update_type,
            sorted(message.keys()) if isinstance(message, dict) else type(message).__name__,
            sorted(body.keys()) if isinstance(body, dict) else type(body).__name__,
            raw_message_id,
            _attachment_summary(update),
        )
        return None

    logger.info("MAX_VOICE_DETECTED chat_id=%s user_id=%s message_id=%s", target.chat_id, target.user_id, raw_message_id)
    try:
        # Start typing before the transcription request, otherwise the user sees
        # no reaction while Whisper/OpenRouter is working.
        await client.send_chat_action(target.chat_id, "typing_on") if target.chat_id else None
        text = await _transcribe_voice_attachment(client, voice_attachment, raw_message_id=raw_message_id, update=update)
        logger.info("MAX_VOICE_TEXT chat_id=%s message_id=%s text=%r", target.storage_chat_id, raw_message_id, text[:200])
    except Exception as exc:
        logger.exception("MAX voice transcription failed chat_id=%s message_id=%s", target.storage_chat_id, raw_message_id)
        text = ""
        # The dialog engine should not receive an empty/failed transcription.
        # Return a synthetic support message so the bot answers politely.
        return (
            target,
            "[Голосовое сообщение не удалось распознать. Попроси клиента отправить текстом или повторить голосовое чуть чётче.]",
            _user_name(sender),
            raw_message_id,
            "message_created",
        )

    return (target, text, _user_name(sender), raw_message_id, "message_created")



def _safe_update_summary(update: Any) -> dict[str, Any]:
    """Small diagnostics without URLs/tokens.

    This is intentionally compact: enough to see how MAX sends voice notes,
    but safe enough to paste into chat without leaking upload links.
    """
    if not isinstance(update, dict):
        return {"type": type(update).__name__}
    message = update.get("message") or update.get("body") or {}
    body = message.get("body") if isinstance(message, dict) else None
    sender = message.get("sender") if isinstance(message, dict) else update.get("user")
    recipient = message.get("recipient") if isinstance(message, dict) else None
    return {
        "update_type": update.get("update_type"),
        "keys": sorted(str(k) for k in update.keys())[:20],
        "chat_id": update.get("chat_id") or (recipient or {}).get("chat_id") if isinstance(recipient, dict) else update.get("chat_id"),
        "user_id": (sender or {}).get("user_id") if isinstance(sender, dict) else update.get("user_id"),
        "message_keys": sorted(str(k) for k in message.keys())[:20] if isinstance(message, dict) else type(message).__name__,
        "body_keys": sorted(str(k) for k in body.keys())[:20] if isinstance(body, dict) else type(body).__name__,
        "message_id": (message.get("mid") or message.get("message_id") or message.get("id")) if isinstance(message, dict) else update.get("message_id"),
        "attachment_summary": _attachment_summary(update),
    }


async def _voice_from_recent_chat_messages(
    client: MaxClient,
    update: dict[str, Any],
) -> tuple[MaxTarget, str, str, str | None, str] | None:
    """Fallback for MAX mobile voice notes hidden from compact updates.

    If /updates advances the marker but does not include a Message body, read the
    latest messages in the chat and inspect their attachments. This is needed for
    voice notes sent from the MAX microphone button in some clients.
    """
    chat_id = _string_or_none(update.get("chat_id"))
    if not chat_id:
        return None

    update_ts_raw = update.get("timestamp")
    try:
        update_ts = int(update_ts_raw) if update_ts_raw is not None else int(time())
    except Exception:
        update_ts = int(time())

    try:
        logger.info("MAX_VOICE_FALLBACK_RECENT_MESSAGES update_type=%s chat_id=%s", update.get("update_type"), chat_id)
        payload = await client.get_messages(chat_id=chat_id, count=5)
    except Exception:
        logger.debug("MAX recent messages fallback failed chat_id=%s", chat_id, exc_info=True)
        return None

    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        logger.info("MAX_VOICE_FALLBACK_NO_MESSAGES chat_id=%s payload_keys=%s", chat_id, sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__)
        return None

    logger.info(
        "MAX_VOICE_FALLBACK_MESSAGES chat_id=%s count=%s summaries=%s",
        chat_id,
        len(messages),
        [_safe_message_summary(item) for item in messages[:3]],
    )

    for message in messages:
        if not isinstance(message, dict):
            continue
        sender = message.get("sender") or {}
        if isinstance(sender, dict) and bool(sender.get("is_bot")):
            continue
        body = message.get("body") or {}
        if _extract_text_from_message_body(body):
            # This is a normal text message, not the hidden voice note we need.
            continue
        raw_message_id = _string_or_none(message.get("mid") or message.get("message_id") or message.get("id"))
        msg_ts_raw = message.get("timestamp")
        try:
            msg_ts = int(msg_ts_raw) if msg_ts_raw is not None else update_ts
        except Exception:
            msg_ts = update_ts
        if abs(update_ts - msg_ts) > 300:
            continue
        voice_attachment = _find_voice_attachment(message)
        if not voice_attachment:
            continue

        target = MaxTarget(chat_id=chat_id, user_id=_string_or_none(sender.get("user_id")) if isinstance(sender, dict) else None)
        logger.info("MAX_VOICE_DETECTED_FROM_RECENT_MESSAGES chat_id=%s message_id=%s", chat_id, raw_message_id)
        try:
            await client.send_chat_action(chat_id, "typing_on")
            text = await _transcribe_voice_attachment(client, voice_attachment, raw_message_id=raw_message_id, update={"message": message})
            logger.info("MAX_VOICE_TEXT chat_id=%s message_id=%s text=%r", chat_id, raw_message_id, text[:200])
            return (target, text, _user_name(sender if isinstance(sender, dict) else {}), raw_message_id, "message_created")
        except Exception:
            logger.exception("MAX recent-message voice transcription failed chat_id=%s message_id=%s", chat_id, raw_message_id)
            return (
                target,
                "[Голосовое сообщение не удалось распознать. Попроси клиента отправить текстом или повторить голосовое чуть чётче.]",
                _user_name(sender if isinstance(sender, dict) else {}),
                raw_message_id,
                "message_created",
            )

    return None


def _safe_message_summary(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {"type": type(message).__name__}
    body = message.get("body") or {}
    sender = message.get("sender") or {}
    return {
        "mid": message.get("mid") or message.get("message_id") or message.get("id"),
        "timestamp": message.get("timestamp"),
        "sender_is_bot": sender.get("is_bot") if isinstance(sender, dict) else None,
        "sender_user_id": sender.get("user_id") if isinstance(sender, dict) else None,
        "body_keys": sorted(str(k) for k in body.keys())[:20] if isinstance(body, dict) else type(body).__name__,
        "has_text": bool(_extract_text_from_message_body(body) if isinstance(body, dict) else False),
        "attachment_summary": _attachment_summary(message),
    }

def _extract_text_from_message_body(body: dict[str, Any] | None) -> str:
    if not isinstance(body, dict):
        return ""
    for key in ("text", "message", "caption"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # Some clients send service text in nested payloads. Keep this conservative.
    payload = body.get("payload")
    if isinstance(payload, dict):
        value = payload.get("text") or payload.get("message")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""



def _attachment_summary(value: Any) -> list[dict[str, Any]]:
    """Return a safe compact summary of attachment-like objects for diagnostics."""
    items: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if len(items) >= 12:
            return
        if isinstance(node, dict):
            if any(key in node for key in ("attachments", "attachment", "payload", "file", "audio", "voice", "media", "type")):
                summary = {
                    "type": node.get("type") or node.get("media_type") or node.get("attachment_type"),
                    "mime": node.get("mime_type") or node.get("mime") or node.get("content_type"),
                    "keys": sorted(str(k) for k in node.keys())[:18],
                }
                # Do not log URLs/tokens. Only show whether URL-like keys exist.
                summary["has_url"] = bool(_find_first_url(node))
                items.append(summary)
            for nested in node.values():
                walk(nested)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return items


def _find_voice_attachment(value: Any) -> dict[str, Any] | None:
    """Find audio/voice attachment in different MAX update shapes.

    MAX docs define message body as text + attachments, but real payloads may
    put media metadata under `body.attachments[*].payload`, `message.attachments`,
    or nested service objects. Keep this recursive and conservative.
    """
    if isinstance(value, dict):
        type_value = str(value.get("type") or value.get("media_type") or value.get("attachment_type") or "").lower()
        name_value = str(value.get("name") or value.get("filename") or value.get("file_name") or "").lower()
        mime_value = str(value.get("mime_type") or value.get("mime") or value.get("content_type") or "").lower()
        audioish_tokens = ("audio", "voice", "audiomsg", "voice_note", "voicemsg", "sound", "opus")
        if (
            type_value in {"audio", "voice", "voice_message", "audio_message", "voice_note", "audio_note", "audiomsg", "voicemsg"}
            or any(token in type_value for token in audioish_tokens)
            or mime_value.startswith("audio/")
            or name_value.endswith((".ogg", ".oga", ".opus", ".mp3", ".wav", ".m4a", ".mp4", ".webm"))
        ):
            return value
        for key in ("attachments", "payload", "body", "media", "file", "audio", "voice", "data"):
            nested = value.get(key)
            found = _find_voice_attachment(nested)
            if found:
                return found
        # Last pass over all values for unknown shapes.
        for nested in value.values():
            found = _find_voice_attachment(nested)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_voice_attachment(item)
            if found:
                return found
    return None


def _find_first_url(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in (
            "url",
            "download_url",
            "downloadUrl",
            "file_url",
            "fileUrl",
            "audio_url",
            "audioUrl",
            "voice_url",
            "voiceUrl",
            "media_url",
            "mediaUrl",
        ):
            raw = value.get(key)
            if isinstance(raw, str) and raw.startswith(("http://", "https://")):
                return raw
        for nested in value.values():
            found = _find_first_url(nested)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_first_url(item)
            if found:
                return found
    return None


def _guess_audio_suffix(attachment: dict[str, Any], url: str | None) -> str:
    blob = json.dumps(attachment, ensure_ascii=False).lower()
    if url:
        blob += " " + url.lower()
    for suffix in (".ogg", ".oga", ".opus", ".mp3", ".wav", ".m4a", ".mp4", ".webm"):
        if suffix in blob:
            return suffix
    mime = str(attachment.get("mime_type") or attachment.get("mime") or attachment.get("content_type") or "").lower()
    if "mpeg" in mime or "mp3" in mime:
        return ".mp3"
    if "wav" in mime:
        return ".wav"
    if "mp4" in mime or "m4a" in mime:
        return ".m4a"
    if "webm" in mime:
        return ".webm"
    return ".ogg"


async def _transcribe_voice_attachment(client: MaxClient, attachment: dict[str, Any], *, raw_message_id: str | None, update: dict[str, Any]) -> str:
    settings = get_settings()
    url = _find_first_url(attachment)

    # Some MAX updates may not include a direct media URL. Try to re-fetch message by id,
    # because GET /messages/{id} can return a fuller Message object than the polling event.
    if not url and raw_message_id:
        try:
            full_message = await client.get_message(raw_message_id)
            richer_attachment = _find_voice_attachment(full_message)
            if richer_attachment:
                attachment = richer_attachment
                url = _find_first_url(richer_attachment)
        except Exception:
            logger.debug("MAX get_message for voice failed message_id=%s", raw_message_id, exc_info=True)

    if not url:
        # Log a safe compact shape, not the whole payload with possible signed URLs.
        logger.warning("MAX voice attachment has no downloadable url keys=%s message_id=%s", list(attachment.keys()), raw_message_id)
        raise VoiceTranscriptionError("MAX voice attachment does not contain a downloadable URL")

    temp_dir = Path(settings.voice_temp_dir)
    if not temp_dir.is_absolute():
        temp_dir = PROJECT_ROOT / temp_dir
    temp_dir.mkdir(parents=True, exist_ok=True)
    suffix = _guess_audio_suffix(attachment, url)
    safe_mid = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw_message_id or str(id(attachment)))
    dest = temp_dir / f"max_voice_{safe_mid}{suffix}"
    max_bytes = max(1, int(settings.voice_max_download_mb or 25)) * 1024 * 1024
    try:
        await client.download_url_to_file(url, dest, max_bytes=max_bytes)
        text = await transcribe_audio_file(dest)
        return text
    finally:
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass


async def _handle_admin_bind_command(client: MaxClient, target: MaxTarget, text: str) -> None:
    settings = get_settings()
    chat_id = target.chat_id or ""
    user_id = target.user_id or ""
    parts = text.strip().split(maxsplit=1)
    provided_secret = parts[1].strip() if len(parts) > 1 else ""

    if settings.max_admin_secret and provided_secret == settings.max_admin_secret:
        _save_admin_target(chat_id=chat_id, user_id=user_id)
        await _send_text(
            client,
            target,
            f"Админ-чат привязан.\n\nMAX_ADMIN_CHAT_ID={chat_id}\nMAX_ADMIN_USER_ID={user_id}",
        )
        return

    if settings.max_admin_secret and provided_secret and provided_secret != settings.max_admin_secret:
        await _send_text(client, target, "Секрет для привязки админ-чата неверный.")
        return

    await _send_text(
        client,
        target,
        "Ваши MAX ID:\n"
        f"chat_id={chat_id}\n"
        f"user_id={user_id}\n\n"
        "Для админ-уведомлений лучше использовать именно chat_id. "
        "Чтобы привязать автоматически, добавьте в .env MAX_ADMIN_SECRET и отправьте сюда: /admin <секрет>",
    )


def _admin_target_path() -> Path:
    settings = get_settings()
    path = Path(settings.max_admin_target_file or "data/max_admin_target.json")
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _merge_admin_target(targets: list[MaxTarget], candidate: MaxTarget) -> None:
    if not candidate.chat_id and not candidate.user_id:
        return

    matching_indexes = [
        index
        for index, current in enumerate(targets)
        if (
            candidate.chat_id
            and current.chat_id
            and str(candidate.chat_id) == str(current.chat_id)
        )
        or (
            candidate.user_id
            and current.user_id
            and str(candidate.user_id) == str(current.user_id)
        )
    ]

    if not matching_indexes:
        targets.append(candidate)
        return

    first = matching_indexes[0]
    current = targets[first]
    targets[first] = MaxTarget(
        chat_id=candidate.chat_id or current.chat_id,
        user_id=candidate.user_id or current.user_id,
    )
    for index in reversed(matching_indexes[1:]):
        targets.pop(index)


def _load_admin_targets() -> list[MaxTarget]:
    path = _admin_target_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []

    raw_targets = data.get("admins")
    if not isinstance(raw_targets, list):
        # Backward compatibility with the former one-admin object.
        raw_targets = [data]

    targets: list[MaxTarget] = []
    for item in raw_targets:
        if not isinstance(item, dict):
            continue
        _merge_admin_target(
            targets,
            MaxTarget(
                chat_id=_string_or_none(item.get("chat_id")),
                user_id=_string_or_none(item.get("user_id")),
            ),
        )
    return targets


def _load_admin_target() -> dict[str, str]:
    """Legacy helper retained for older imports/tests."""
    targets = _load_admin_targets()
    if not targets:
        return {}
    first = targets[0]
    return {"chat_id": str(first.chat_id or ""), "user_id": str(first.user_id or "")}


def _save_admin_target(*, chat_id: str, user_id: str) -> None:
    path = _admin_target_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    targets = _load_admin_targets()
    _merge_admin_target(
        targets,
        MaxTarget(
            chat_id=_string_or_none(chat_id),
            user_id=_string_or_none(user_id),
        ),
    )
    payload = {
        "admins": [
            {
                "chat_id": str(target.chat_id or ""),
                "user_id": str(target.user_id or ""),
            }
            for target in targets
        ]
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _user_name(user: dict[str, Any] | None) -> str:
    user = user or {}
    parts = [str(user.get("first_name") or "").strip(), str(user.get("last_name") or "").strip()]
    name = " ".join(part for part in parts if part).strip()
    return name or str(user.get("name") or user.get("username") or "")


def _string_or_none(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


async def _send_text(client: MaxClient, target: MaxTarget, text: str) -> None:
    settings = get_settings()
    fmt = settings.max_message_format if settings.max_message_format in {"markdown", "html"} else None
    chunks = _split_message(text, limit=3900)
    for chunk in chunks:
        await client.send_message(chat_id=target.chat_id, user_id=None if target.chat_id else target.user_id, text=chunk, text_format=fmt)


async def _send_requested_media(client: MaxClient, target: MaxTarget, requested_media: list[str]) -> None:
    settings = get_settings()
    if not settings.max_media_enabled:
        return
    paths = paths_for_requested_media(requested_media)
    logger.info("MAX_MEDIA_SEND requested_media=%s paths=%s", requested_media, [str(path) for path in paths])
    if not paths:
        return

    # MAX supports an attachments array in POST /messages. Sending all images in one
    # message makes the client render them as one grouped media block instead of a
    # noisy sequence of separate messages. If the platform rejects the batch, fall
    # back to separate images so the customer still receives the photos.
    try:
        if len(paths) > 1 and getattr(settings, "max_media_group_enabled", True):
            await client.send_images(
                chat_id=target.chat_id,
                user_id=None if target.chat_id else target.user_id,
                paths=[Path(path) for path in paths],
            )
            return
    except Exception:
        logger.exception("Failed to send MAX media group; falling back to single images paths=%s", [str(path) for path in paths])

    for path in paths:
        try:
            await client.send_image(chat_id=target.chat_id, user_id=None if target.chat_id else target.user_id, path=Path(path))
        except Exception:
            logger.exception("Failed to send MAX related media path=%s", path)


async def _typing_loop(client: MaxClient, target: MaxTarget) -> None:
    if not target.chat_id:
        return
    while True:
        try:
            await client.send_chat_action(target.chat_id, "typing_on")
        except Exception:
            logger.debug("Failed to send MAX typing action", exc_info=True)
        await asyncio.sleep(2 if is_llm_rate_limited() else 4)


async def _safe_handle(chat_id: str, user_name: str, text: str) -> str:
    try:
        return await asyncio.to_thread(handle_text, chat_id, user_name, text, platform="max")
    except Exception as exc:
        logger.exception("Dialog handling failed chat_id=%s", chat_id)
        sqlite.log_system("ERROR", "dialog_failed", str(exc), {"chat_id": chat_id, "text": text})
        sqlite.enqueue_admin_notification(
            f"Ошибка в диалоге {chat_id}.\nСообщение клиента: {text}\nОшибка: {exc}",
            chat_id=chat_id,
        )
        return ""


async def _payment_status_loop(client: MaxClient) -> None:
    settings = get_settings()
    while True:
        try:
            if settings.payment_status_loop_enabled:
                events = await asyncio.to_thread(sync_paid_bookings, platform="max")
                for event in events:
                    await client.send_message(
                        chat_id=event["chat_id"],
                        text=event["message"],
                        text_format=settings.max_message_format,
                    )
                    try:
                        sqlite.add_message(
                            str(event["chat_id"]),
                            "assistant",
                            str(event["message"]),
                            raw={"source": "payment_status_loop", "platform": "max"},
                        )
                    except Exception:
                        # Delivery already succeeded. A transcript logging problem
                        # must not turn a paid booking into a send failure.
                        logger.exception(
                            "PAYMENT_CUSTOMER_MESSAGE_LOG_FAILED chat_id=%s",
                            event.get("chat_id"),
                        )
                if events:
                    logger.info("Processed paid bookings count=%s", len(events))
        except Exception as exc:
            logger.exception("Payment status loop failed")
            try:
                sqlite.enqueue_admin_notification(
                    "Ошибка API в цикле проверки оплат.\n"
                    f"Источник: MAX bot\n"
                    f"Ошибка: {exc}",
                    chat_id=None,
                )
            except Exception:
                logger.exception("Failed to enqueue payment loop API error notification")
        await asyncio.sleep(max(5, int(settings.payment_status_sync_interval_seconds or 10)))


# MULTIPLE_MAX_ADMINS
def _parse_max_admin_ids(raw: object) -> list[str]:
    text = str(raw or "")

    for separator in (",", ";"):
        text = text.replace(separator, " ")

    result: list[str] = []

    for token in text.split():
        value = token.strip()

        if not value:
            continue

        if not value.isdigit():
            logger.warning(
                "INVALID_MAX_ADMIN_ID value=%r",
                value,
            )
            continue

        if value not in result:
            result.append(value)

    return result


def _configured_admin_targets(settings: Any) -> list[MaxTarget]:
    targets = _load_admin_targets()
    chat_ids = _parse_max_admin_ids(settings.max_admin_chat_id)
    user_ids = _parse_max_admin_ids(settings.max_admin_user_id)

    # When both environment lists have the same length, their order defines the
    # explicit chat_id <-> user_id mapping. Legacy one-sided lists remain valid.
    if chat_ids and user_ids and len(chat_ids) == len(user_ids):
        for chat_id, user_id in zip(chat_ids, user_ids):
            _merge_admin_target(
                targets,
                MaxTarget(chat_id=chat_id, user_id=user_id),
            )
    else:
        for chat_id in chat_ids:
            _merge_admin_target(targets, MaxTarget(chat_id=chat_id))
        for user_id in user_ids:
            _merge_admin_target(targets, MaxTarget(user_id=user_id))

    return targets


async def _send_admin_notification_to_target(
    client: MaxClient,
    target: MaxTarget,
    message_text: str,
) -> str:
    try:
        await client.send_message(
            chat_id=(None if target.user_id else target.chat_id),
            user_id=target.user_id,
            text=message_text,
        )
        return "user_id" if target.user_id else "chat_id"
    except MaxApiError:
        if not (target.chat_id and target.user_id):
            raise
        logger.warning(
            "ADMIN_MAX_USER_SEND_FAILED_RETRY_PAIRED_CHAT "
            "chat_id=%s user_id=%s",
            target.chat_id,
            target.user_id,
        )
        await client.send_message(
            chat_id=target.chat_id,
            text=message_text,
        )
        return "paired_chat_id_fallback"


async def _admin_notification_loop(client: MaxClient) -> None:
    settings = get_settings()
    next_allowed_after: dict[str, float] = {}

    while True:
        try:
            targets = _configured_admin_targets(settings)

            if targets:
                notifications = await asyncio.to_thread(
                    sqlite.list_pending_admin_notifications
                )

                for item in notifications:
                    message_text = item.get("message") or ""

                    if len(message_text) > 3500:
                        message_text = (
                            message_text[:3500]
                            + "\n\n...сообщение обрезано, "
                            "полный текст в логах сервера..."
                        )

                    send_errors: list[str] = []
                    delivered: list[str] = []

                    for target in targets:
                        target_label = (
                            f"chat_id={target.chat_id or '-'}"
                            f",user_id={target.user_id or '-'}"
                        )

                        blocked_until = next_allowed_after.get(
                            target_label,
                            0.0,
                        )

                        if monotonic() < blocked_until:
                            send_errors.append(
                                f"{target_label}: временная пауза"
                            )
                            continue

                        try:
                            route = await _send_admin_notification_to_target(
                                client,
                                target,
                                message_text,
                            )

                            delivered.append(f"{target_label} via={route}")

                        except MaxApiError as exc:
                            next_allowed_after[target_label] = (
                                monotonic()
                                + max(
                                    30,
                                    int(
                                        settings
                                        .max_admin_error_sleep_seconds
                                        or 60
                                    ),
                                )
                            )

                            logger.exception(
                                "ADMIN_MAX_SEND_FAILED "
                                "target=%s status=%s",
                                target_label,
                                exc.status_code,
                            )

                            send_errors.append(
                                f"{target_label}: {exc}"
                            )

                        except Exception as exc:
                            next_allowed_after[target_label] = (
                                monotonic() + 60
                            )

                            logger.exception(
                                "ADMIN_MAX_SEND_FAILED target=%s",
                                target_label,
                            )

                            send_errors.append(
                                f"{target_label}: {exc}"
                            )

                    if send_errors:
                        logger.error(
                            "ADMIN_MAX_NOTIFICATION_NOT_MARKED_SENT "
                            "notification_id=%s delivered=%s errors=%s",
                            item["id"],
                            delivered,
                            send_errors,
                        )
                        continue

                    await asyncio.to_thread(
                        sqlite.mark_admin_notification_sent,
                        int(item["id"]),
                    )

                    logger.info(
                        "ADMIN_MAX_SENT "
                        "notification_id=%s targets=%s",
                        item["id"],
                        delivered,
                    )

        except Exception:
            logger.exception("Admin notification loop failed")

        await asyncio.sleep(5)



async def _availability_cache_loop() -> None:
    settings = get_settings()
    while True:
        await asyncio.sleep(max(60, int(settings.yclients_sync_interval_seconds or 3600)))
        if not settings.yclients_sync_enabled:
            continue
        try:
            await asyncio.to_thread(
                refresh_availability_cache,
                days=settings.yclients_sync_days_forward,
                max_seconds=180,
                reason="periodic",
            )
        except Exception as exc:
            logger.exception("Availability cache refresh failed")
            try:
                sqlite.enqueue_admin_notification(
                    "Ошибка API при обновлении доступности YClients.\n"
                    f"Источник: MAX bot\n"
                    f"Ошибка: {exc}",
                    chat_id=None,
                )
            except Exception:
                logger.exception("Failed to enqueue availability API error notification")
        try:
            from app.integrations.yclients_sync_service import sync_records_once

            await asyncio.to_thread(
                sync_records_once,
                days_back=settings.yclients_sync_days_back,
                days_forward=settings.yclients_sync_days_forward,
            )
        except Exception as exc:
            logger.exception("YCLIENTS sync failed")
            try:
                sqlite.enqueue_admin_notification(
                    "Ошибка API при синхронизации записей YClients.\n"
                    f"Источник: MAX bot\n"
                    f"Ошибка: {exc}",
                    chat_id=None,
                )
            except Exception:
                logger.exception("Failed to enqueue YCLIENTS sync API error notification")


async def _startup_availability_cache_refresh() -> None:
    settings = get_settings()
    await asyncio.sleep(max(0, int(settings.startup_availability_refresh_delay_seconds or 10)))
    try:
        logger.info("Initial availability cache background check started")
        await asyncio.to_thread(
            refresh_availability_cache,
            days=settings.yclients_sync_days_forward,
            max_seconds=180,
            reason="initial_force",
        )
        from app.integrations.yclients_sync_service import sync_records_once

        await asyncio.to_thread(
            sync_records_once,
            days_back=settings.yclients_sync_days_back,
            days_forward=settings.yclients_sync_days_forward,
        )
    except Exception:
        logger.exception("Initial YCLIENTS sync failed")


async def _watchlist_loop(client: MaxClient) -> None:
    settings = get_settings()
    while True:
        try:
            if settings.watchlist_loop_enabled:
                events = await asyncio.to_thread(check_active_watchlist, platform="max")
                sent_count = 0
                for event in events:
                    try:
                        message = await asyncio.to_thread(
                            render_watchlist_triggered_message,
                            event,
                        )

                        if not message:
                            logger.warning(
                                "WATCHLIST_NOTIFY_EMPTY id=%s chat_id=%s",
                                event.get("id"),
                                event.get("chat_id"),
                            )
                            continue

                        response = await client.send_message(
                            chat_id=event["chat_id"],
                            text=message,
                            text_format=settings.max_message_format,
                        )

                        created_message = response.get("message") or {}
                        body = created_message.get("body") or {}
                        recipient = created_message.get("recipient") or {}

                        message_id = body.get("mid")
                        recipient_chat_id = str(recipient.get("chat_id") or "")

                        if not message_id or recipient_chat_id != str(event["chat_id"]):
                            raise RuntimeError(
                                "MAX did not confirm watchlist message creation: "
                                f"watchlist_id={event.get('id')} response={response}"
                            )

                        logger.info(
                            "WATCHLIST_MAX_CONFIRMED id=%s chat_id=%s mid=%s",
                            event.get("id"),
                            event.get("chat_id"),
                            message_id,
                        )
                    except Exception:
                        # Do not mark the watchlist as notified. The next loop
                        # iteration will retry after availability is checked again.
                        logger.exception(
                            "WATCHLIST_NOTIFY_FAILED id=%s chat_id=%s",
                            event.get("id"),
                            event.get("chat_id"),
                        )
                        continue

                    sqlite.mark_watchlist_notified(int(event["id"]))
                    sent_count += 1
                    logger.info(
                        "WATCHLIST_NOTIFY_SENT id=%s chat_id=%s",
                        event.get("id"),
                        event.get("chat_id"),
                    )
                if sent_count:
                    logger.info("WATCHLIST events sent count=%s", sent_count)
        except Exception:
            logger.exception("Watchlist loop failed")
        await asyncio.sleep(max(10, int(settings.watchlist_loop_interval_seconds or 15)))


def _split_message(text: str, *, limit: int) -> list[str]:
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        chunks.append(rest)
    return chunks
