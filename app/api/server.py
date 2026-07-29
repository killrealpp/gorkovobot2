from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.public_catalog import register_public_catalog_routes
from app.core.config import get_settings


def create_public_api_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="MaxBot Public API", version="public_catalog.v1")

    origins = _cors_origins(settings.public_api_cors_origins)
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=_cors_methods(settings.catalog_admin_local_enabled),
            allow_headers=["*"],
        )

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "ok": True,
            "service": "maxbot-public-api",
            "catalog": "public_catalog.v1",
        }

    register_public_catalog_routes(app)
    if settings.catalog_admin_local_enabled:
        from app.api.catalog_admin import register_catalog_admin_routes

        register_catalog_admin_routes(app)
    return app


def _cors_origins(raw_value: str | None) -> list[str]:
    return [item.strip() for item in str(raw_value or "").split(",") if item.strip()]


def _cors_methods(admin_enabled: bool) -> list[str]:
    if admin_enabled:
        return ["GET", "PATCH", "PUT", "POST", "OPTIONS"]
    return ["GET", "OPTIONS"]


app = create_public_api_app()


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, str(settings.log_level or "INFO").upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("Public API requires fastapi and uvicorn. Install requirements.txt") from exc

    uvicorn.run(
        create_public_api_app(),
        host=settings.public_api_host,
        port=int(settings.public_api_port),
        log_level=str(settings.log_level or "info").lower(),
    )


if __name__ == "__main__":
    main()
