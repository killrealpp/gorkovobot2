from __future__ import annotations

import re
from pathlib import Path

from app.data.admin_profile import media_path_for_key, resolve_media_key


# Сколько фото максимум отправлять одним ответом. Telegram media group ограничен 10 файлами.
MAX_MEDIA_PER_REPLY = 10


def extract_media_titles_from_reply(text: str) -> list[str]:
    """Достаёт явный маркер действия от LLM из ответа бота."""

    marker = re.search(
        r"(?:^|\n)\s*(?:вот\s+)?фото\s+вариантов\s*:\s*(.+)",
        text,
        flags=re.IGNORECASE,
    )
    if not marker:
        return []

    raw = marker.group(1).strip().splitlines()[0].strip()
    requested = [item.strip() for item in raw.split(",") if item.strip()]
    return _resolve_titles(requested)


def remove_media_marker_from_reply(text: str) -> str:
    """Оставлено для совместимости. Сейчас telegram.py маркер не удаляет."""

    lines: list[str] = []
    for line in text.splitlines():
        normalized = line.lower().replace("ё", "е").strip()
        if re.match(r"^(?:вот\s+)?фото\s+вариантов\s*:", normalized):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def paths_for_requested_media(requested_media: list[str]) -> list[Path]:
    """Возвращает фото по явным названиям от LLM/requested_media."""

    paths: list[Path] = []
    for item in requested_media or []:
        key = resolve_media_key(str(item).strip())
        if not key:
            continue
        path = media_path_for_key(key)
        if path:
            paths.append(path)
    return _existing(paths)[:MAX_MEDIA_PER_REPLY]


def _resolve_titles(requested: list[str]) -> list[str]:
    result: list[str] = []
    for title in requested:
        matched = resolve_media_key(title)
        if matched and matched not in result:
            result.append(matched)
    return result


def _normalize_title(value: str) -> str:
    # Kept for compatibility with historical tests and future fallback use.
    value = value.lower().replace("ё", "е")
    value = value.replace("№", "")
    value = value.replace("#", "")
    value = value.replace(".", "")
    return " ".join(value.split())


def _existing(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path.exists() and path not in seen:
            result.append(path)
            seen.add(path)
    return result
