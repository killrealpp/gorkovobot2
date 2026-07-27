from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class VoiceTranscriptionError(RuntimeError):
    pass


def _api_key() -> str:
    settings = get_settings()
    return (
        settings.voice_transcription_api_key
        or settings.openrouter_api_key
        or settings.openai_api_key
        or settings.ai_api_key
        or settings.api_key
        or ""
    ).strip()


def _base_url() -> str:
    settings = get_settings()
    return (
        settings.voice_transcription_base_url
        or settings.openai_base_url
        or "https://api.openai.com/v1"
    ).rstrip("/")


async def transcribe_audio_file(path: str | Path) -> str:
    """Transcribe a MAX voice/audio file through an OpenAI-compatible endpoint.

    Works with OpenAI directly and with providers that implement the compatible
    `/audio/transcriptions` endpoint. For OpenRouter, set:
      VOICE_TRANSCRIPTION_BASE_URL=https://openrouter.ai/api/v1
      VOICE_TRANSCRIPTION_API_KEY=<OpenRouter key>
      VOICE_TRANSCRIPTION_MODEL=<audio transcription model exposed by provider>

    If OpenRouter does not expose speech-to-text for the selected model, use
    OpenAI-compatible settings with `whisper-1` or `gpt-4o-mini-transcribe`.
    """
    settings = get_settings()
    file_path = Path(path)
    if not file_path.exists():
        raise VoiceTranscriptionError(f"Audio file not found: {file_path}")

    key = _api_key()
    if not key:
        raise VoiceTranscriptionError("Voice transcription API key is empty")

    url = f"{_base_url()}/audio/transcriptions"
    model = (settings.voice_transcription_model or "whisper-1").strip()
    response_format = (settings.voice_transcription_response_format or "json").strip()
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"

    data: dict[str, Any] = {
        "model": model,
        "response_format": response_format,
        "temperature": "0",
    }
    if settings.voice_transcription_language:
        data["language"] = settings.voice_transcription_language
    # Keep prompt short and domain-specific: it improves short Russian booking voice notes.
    data["prompt"] = "Распознай короткое голосовое сообщение клиента на русском про бронирование: дата, время, объект, гости, оплата, перенос или отмена. Верни только текст."

    logger.info("VOICE_TRANSCRIBE_START file=%s model=%s base_url=%s", file_path.name, model, _base_url())
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.voice_transcription_timeout_seconds, connect=15.0), trust_env=bool(settings.http_trust_env)) as client:
            with file_path.open("rb") as f:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {key}"},
                    data=data,
                    files={"file": (file_path.name, f, mime_type)},
                )
    except httpx.HTTPError as exc:
        raise VoiceTranscriptionError(f"Voice transcription request failed: {exc}") from exc

    if response.status_code >= 400:
        raise VoiceTranscriptionError(f"Voice transcription HTTP {response.status_code}: {response.text[:700]}")

    text = ""
    if response_format == "text":
        text = response.text.strip()
    else:
        try:
            payload = response.json()
        except ValueError as exc:
            raise VoiceTranscriptionError(f"Voice transcription returned non-JSON: {response.text[:700]}") from exc
        if isinstance(payload, dict):
            raw = payload.get("text") or payload.get("transcript") or payload.get("transcription")
            if isinstance(raw, str):
                text = raw.strip()
        elif isinstance(payload, str):
            text = payload.strip()

    if not text:
        raise VoiceTranscriptionError("Voice transcription returned empty text")
    logger.info("VOICE_TRANSCRIBE_OK chars=%s text=%r", len(text), text[:200])
    return text
