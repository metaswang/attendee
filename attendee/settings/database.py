from __future__ import annotations

import os

import dj_database_url


def configured_database_url() -> str | None:
    """Return the service database URL, falling back to Voxella's shared config."""
    return os.getenv("DATABASE_URL") or os.getenv("DB__URL") or None


def django_database_config(*, conn_max_age: int = 0, conn_health_checks: bool = False, ssl_require: bool | None = None) -> dict | None:
    database_url = configured_database_url()
    if not database_url:
        return None

    # Voxella API uses SQLAlchemy's asyncpg dialect; Django needs the standard
    # PostgreSQL scheme while keeping the same host, database, and credentials.
    if database_url.startswith("postgresql+asyncpg://"):
        database_url = "postgresql://" + database_url.removeprefix("postgresql+asyncpg://")

    options: dict[str, object] = {
        "conn_max_age": conn_max_age,
        "conn_health_checks": conn_health_checks,
    }
    if ssl_require is not None:
        options["ssl_require"] = ssl_require
    return dj_database_url.parse(database_url, **options)
