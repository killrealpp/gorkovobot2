from __future__ import annotations

from typing import Any


SECRET_LIKE_KEY_MARKERS: tuple[str, ...] = (
    "payment",
    "post_payment",
    "postpayment",
    "token",
    "secret",
    "api_key",
    "apikey",
    "access_key",
    "accesskey",
    "password",
    "supabase",
    "service_role",
    "servicerole",
    "anon_key",
    "anonkey",
    "yclients",
    "yookassa",
    "payment_secret_key",
    "yclients_partner_token",
    "yclients_user_token",
    "openrouter_api_key",
    "max_bot_token",
    "db_password",
)

FORBIDDEN_PUBLIC_KEYS: frozenset[str] = frozenset(
    {
        "integration_refs",
        "integrationrefs",
        "private_refs",
        "privaterefs",
        "payment",
        "post_payment",
        "postpayment",
        "post_payment_instruction",
        "post_payment_instruction_key",
        "yclients_service_id",
        "yclients_staff_id",
        "yclients_service_ids",
        "yclients_staff_ids",
        "supabase_url",
        "supabase_anon_key",
        "supabase_service_role_key",
        "service_role_key",
    }
)


def is_forbidden_public_key(key: object) -> bool:
    """Return True when a key must not appear in public/admin draft payloads."""

    normalized = str(key).lower().strip()
    return normalized in FORBIDDEN_PUBLIC_KEYS or any(marker in normalized for marker in SECRET_LIKE_KEY_MARKERS)


def collect_forbidden_public_paths(value: Any, *, path: str = "$") -> list[str]:
    """Collect paths to secret-like keys in a nested public DTO candidate."""

    paths: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            nested_path = f"{path}.{key}"
            if is_forbidden_public_key(key):
                paths.append(nested_path)
            paths.extend(collect_forbidden_public_paths(nested, path=nested_path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            paths.extend(collect_forbidden_public_paths(nested, path=f"{path}[{index}]"))
    return paths


def public_payload_has_forbidden_keys(value: Any) -> bool:
    return bool(collect_forbidden_public_paths(value))


def ensure_public_payload_safe(value: Any) -> None:
    paths = collect_forbidden_public_paths(value)
    if paths:
        raise ValueError("forbidden public catalog keys: " + ", ".join(paths))
