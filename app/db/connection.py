from contextlib import contextmanager
import logging
from pathlib import Path
import time
from typing import Generator

import psycopg2
from psycopg2.extensions import connection as PgConnection
from psycopg2.extras import RealDictCursor

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _connect_kwargs() -> dict:
    settings = get_settings()
    sslrootcert = Path(settings.db_sslrootcert).expanduser()
    return {
        "host": settings.db_host,
        "port": settings.db_port,
        "dbname": settings.db_name,
        "user": settings.db_user,
        "password": settings.db_password,
        "sslmode": settings.db_sslmode,
        "sslrootcert": str(sslrootcert),
        "target_session_attrs": settings.db_target_session_attrs,
        "connect_timeout": settings.db_connect_timeout,
        "cursor_factory": RealDictCursor,
    }


def connect() -> PgConnection:
    last_error = None
    for attempt in range(3):
        try:
            return psycopg2.connect(**_connect_kwargs())
        except psycopg2.OperationalError as exc:
            last_error = exc
            logger.warning("DB connect attempt %s failed: %s", attempt + 1, exc)
            if attempt == 2:
                raise
            time.sleep(1 + attempt)
    raise last_error or RuntimeError("Database connection failed")


@contextmanager
def get_connection() -> Generator[PgConnection, None, None]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        if not conn.closed:
            conn.rollback()
        raise
    finally:
        conn.close()
