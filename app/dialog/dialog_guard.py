from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.data.admin_profile import booking_question
from app.data.services import load_services, service_variants
from app.data.services import service_title as profile_service_title
from app.dialog.state import BookingDraft
from app.storage import sqlite


@dataclass
class GuardResult:
    handled: bool
    text: str | None = None
    draft_patch: dict[str, Any] | None = None


def _norm(text: str) -> str:
    text = text.lower().replace("ё", "е").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _service_title(service_type: str | None) -> str:
    return profile_service_title(service_type) if service_type else "объект"


def _bathhouse_capacity() -> int:
    service = load_services().get("bathhouse") or {}
    return int(service.get("capacity_max") or 10)


def _bathhouse_info() -> str:
    cap = _bathhouse_capacity()
    return (
        f"У нас есть баня с бассейном, вместимость — до {cap} человек.\n"
        "Можно забронировать на 3, 4, 5, 6 или 7 часов. "
        "Цена зависит от дня недели и длительности."
    )


def _bathhouse_prices() -> str:
    variants = service_variants("bathhouse")
    seen: list[str] = []
    for variant in variants:
        title = str(variant.get("title") or "Баня с бассейном")
        price = variant.get("price")
        if not price:
            continue
        label = f"{title} — {int(price):,} ₽".replace(",", " ")
        if label not in seen:
            seen.append(label)
    return "\n".join(f"— {item}" for item in seen[:10])


def _large_company_options(guests: int | None = None) -> str:
    services = load_services()
    rows: list[tuple[int, int, str]] = []
    for variant in services.get("gazebo", {}).get("variants") or []:
        cap = int(variant.get("capacity_max") or 0)
        price = int(variant.get("price") or 0)
        title = str(variant.get("title") or "")
        if title and cap and (guests is None or cap >= guests):
            rows.append((price or 999999, cap, f"{title} — до {cap} человек, {price:,} ₽".replace(",", " ")))
    warm = services.get("warm_gazebo") or {}
    if warm.get("capacity_max") and (guests is None or int(warm["capacity_max"]) >= guests):
        rows.append((int(warm.get("price") or 999999), int(warm["capacity_max"]), f"Тёплая беседка — до {warm['capacity_max']} человек, {int(warm.get('price') or 0):,} ₽".replace(",", " ")))
    if guests is None or guests <= 20:
        rows.append((999998, 20, "Гостевой дом — по времени от 4 до 7 часов или сутки"))
    rows.sort(key=lambda x: (x[0], x[1]))
    return "\n".join(f"— {row[2]}" for row in rows[:5])


def _extract_guest_count(text: str) -> int | None:
    t = _norm(text)
    numbers = [int(x) for x in re.findall(r"\d+", t)]
    if numbers:
        return max(numbers)
    words = {"десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50}
    found = [value for word, value in words.items() if word in t]
    return max(found) if found else None


def _find_nearest_cached_date(service_type: str, after_date: str | None = None, service_variant: str | None = None) -> str | None:
    try:
        rows = sqlite.list_availability_rows(service_type=service_type, limit=5000)
    except Exception:
        return None
    if after_date:
        try:
            min_date = datetime.fromisoformat(after_date).date()
        except ValueError:
            min_date = datetime.now().date()
    else:
        min_date = datetime.now().date()
    dates_with_slots: set[str] = set()
    for row in rows:
        if service_variant and row.get("title") != service_variant:
            continue
        if row.get("status") != "free" or not row.get("time"):
            continue
        raw_date = str(row.get("date") or "")
        try:
            date_obj = datetime.fromisoformat(raw_date).date()
        except ValueError:
            continue
        if date_obj >= min_date:
            dates_with_slots.add(raw_date)
    sorted_dates = sorted(dates_with_slots)
    return sorted_dates[0] if sorted_dates else None


def _format_date(date: str | None) -> str:
    if not date:
        return "дату пока не выбрали"
    try:
        dt = datetime.fromisoformat(date)
        months = {1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня", 7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"}
        return f"{dt.day} {months[dt.month]}"
    except ValueError:
        return date


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _is_bathhouse_question(t: str) -> bool:
    return "бан" in t and _has_any(t, ("какие", "покажи", "есть", "варианты", "расскажи"))


def _is_capacity_question(t: str) -> bool:
    return _has_any(t, ("сколько", "до скольки", "на сколько", "скок")) and _has_any(t, ("человек", "мест", "можно", "влез", "рассчитан", "помест"))


def _is_price_question(t: str) -> bool:
    return _has_any(t, ("сколько стоит", "цена", "стоимость")) and not _has_any(t, ("почему",))


def _wants_nearest_date(t: str) -> bool:
    return (_has_any(t, ("ближайш", "самое раннее", "раньше всего", "первое свобод", "когда свобод")) and _has_any(t, ("свобод", "запис", "брон", "число", "дат"))) or (_has_any(t, ("когда", "какое", "ближайш")) and _has_any(t, ("свобод", "есть")))


def _asks_question(t: str) -> bool:
    question_words = ("сколько", "какие", "какая", "какой", "что", "где", "когда", "зачем", "почему", "до скольки", "на какое")
    return "?" in t or _has_any(t, question_words)


def handle_dialog_guard(text: str, draft: BookingDraft) -> GuardResult:
    t = _norm(text)
    service_type = draft.service_type

    # Только проверка времени и ближайшей даты
    if re.search(r"\b\d{1,2}[:\s]\d{2}\b", text) and draft.service_type:
        return GuardResult(False)

    if service_type and _wants_nearest_date(t):
        nearest = _find_nearest_cached_date(service_type, after_date=draft.date, service_variant=draft.service_variant)
        if nearest:
            return GuardResult(True, f"Ближайшая свободная дата — {_format_date(nearest)}.", {"date": nearest})
        return GuardResult(False)

    return GuardResult(False)


def next_step_question(draft: BookingDraft) -> str:
    return booking_question(draft.next_step())
