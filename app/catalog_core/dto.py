from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


JsonObject = dict[str, Any]


@dataclass(frozen=True)
class RawProfileDTO:
    """Raw profile/catalog input before Catalog Core resolution.

    This DTO is intentionally generic for Block A: it can represent the current
    YAML-backed profile or a future backend-owned source without adding a
    runtime dependency on either storage or integrations.
    """

    payload: JsonObject = field(default_factory=dict)
    source: str = "business_profile.admin_profile"
    schema_version: str = "raw_profile.v1"

    def to_dict(self) -> JsonObject:
        return asdict(self)


RawCatalogInput = RawProfileDTO


@dataclass(frozen=True)
class DraftOverridesDTO:
    """Local/admin-editable draft overlay shape.

    Values are intentionally dictionaries until the future resolver owns a full
    typed domain model. The allowed fields are validated in validation.py.
    """

    categories: JsonObject = field(default_factory=dict)
    facilities: JsonObject = field(default_factory=dict)
    tariffs: JsonObject = field(default_factory=dict)
    media: JsonObject = field(default_factory=dict)
    schema_version: str = "draft_overrides.v1"

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass(frozen=True)
class CatalogPrivateRefs:
    """Backend-only private reference bucket.

    This object must never be copied into a public DTO. It is separated here so
    later YCLIENTS/payment/Supabase mappings have an explicit private home.
    """

    integration_refs: JsonObject = field(default_factory=dict)
    payment_refs: JsonObject = field(default_factory=dict)
    supabase_refs: JsonObject = field(default_factory=dict)
    internal_refs: JsonObject = field(default_factory=dict)
    private: bool = True

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass(frozen=True)
class ResolvedCatalogDTO:
    """Internal resolved catalog shadow model.

    By default, to_dict() drops private_refs. Callers must explicitly opt in to
    include private references, which keeps the safe default aligned with the
    public catalog boundary.
    """

    business: JsonObject = field(default_factory=dict)
    categories: list[JsonObject] = field(default_factory=list)
    services: list[JsonObject] = field(default_factory=list)
    facilities: list[JsonObject] = field(default_factory=list)
    variants: list[JsonObject] = field(default_factory=list)
    tariffs: list[JsonObject] = field(default_factory=list)
    media: list[JsonObject] = field(default_factory=list)
    availability_snapshot: JsonObject = field(default_factory=dict)
    warnings: list[JsonObject] = field(default_factory=list)
    private_refs: CatalogPrivateRefs | None = None
    schema_version: str = "resolved_catalog.shadow.v1"

    def to_dict(self, *, include_private: bool = False) -> JsonObject:
        payload = asdict(self)
        if not include_private:
            payload.pop("private_refs", None)
        return payload


@dataclass(frozen=True)
class PublicCatalogDTO:
    """Public frontend read model.

    This DTO is data-only and deliberately has no private reference slot.
    """

    business: JsonObject = field(default_factory=dict)
    categories: list[JsonObject] = field(default_factory=list)
    services: list[JsonObject] = field(default_factory=list)
    facilities: list[JsonObject] = field(default_factory=list)
    variants: list[JsonObject] = field(default_factory=list)
    tariffs: list[JsonObject] = field(default_factory=list)
    media: list[JsonObject] = field(default_factory=list)
    availability_state: JsonObject = field(default_factory=dict)
    publicBookingPolicy: JsonObject = field(default_factory=dict)
    warnings: list[JsonObject] = field(default_factory=list)
    schema_version: str = "public_catalog.v1"

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass(frozen=True)
class BotPreviewDTO:
    """Read-only bot/admin preview model for one facility."""

    facility_id: str
    facility: JsonObject = field(default_factory=dict)
    tariffs: list[JsonObject] = field(default_factory=list)
    media: list[JsonObject] = field(default_factory=list)
    text: str = ""
    mode: str = "maxbot-local-admin"
    schema_version: str = "bot_preview.shadow.v1"

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass(frozen=True)
class ValidationResultDTO:
    """Stable validation result contract: ok/errors/warnings."""

    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> JsonObject:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }
