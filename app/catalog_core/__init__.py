"""Shadow-mode Catalog Core foundation.

This package intentionally contains only pure DTO, privacy, and validation
helpers. It is not wired into the live public catalog builder, bot reader,
booking flow, payment flow, YCLIENTS integrations, or storage adapters.
"""

from app.catalog_core.dto import (
    BotPreviewDTO,
    CatalogPrivateRefs,
    DraftOverridesDTO,
    PublicCatalogDTO,
    RawCatalogInput,
    RawProfileDTO,
    ResolvedCatalogDTO,
    ValidationResultDTO,
)
from app.catalog_core.privacy import (
    FORBIDDEN_PUBLIC_KEYS,
    SECRET_LIKE_KEY_MARKERS,
    collect_forbidden_public_paths,
    is_forbidden_public_key,
    public_payload_has_forbidden_keys,
)
from app.catalog_core.validation import (
    ALLOWED_SECTION_FIELDS,
    OVERRIDE_SECTIONS,
    validate_draft_like,
)

__all__ = [
    "ALLOWED_SECTION_FIELDS",
    "BotPreviewDTO",
    "CatalogPrivateRefs",
    "DraftOverridesDTO",
    "FORBIDDEN_PUBLIC_KEYS",
    "OVERRIDE_SECTIONS",
    "PublicCatalogDTO",
    "RawCatalogInput",
    "RawProfileDTO",
    "ResolvedCatalogDTO",
    "SECRET_LIKE_KEY_MARKERS",
    "ValidationResultDTO",
    "collect_forbidden_public_paths",
    "is_forbidden_public_key",
    "public_payload_has_forbidden_keys",
    "validate_draft_like",
]
