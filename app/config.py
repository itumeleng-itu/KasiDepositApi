"""Settings, read from the environment (and `.env` at the project root).

`DATABASE_URL` and `ENVIRONMENT` have no defaults: if either is missing the
app refuses to start. `ENABLE_DEMO_ROUTES` defaults to true only in
development, and production refuses to start with it switched on. Test-only variables (`TEST_DATABASE_URL`,
`TEST_DATABASE_SCHEMA`) are deliberately *not* settings here — only the test
suite reads them (see `tests/conftest.py`), so a production deployment never
needs them.
"""

import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

Environment = Literal["development", "test", "production"]

_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def normalize_database_url(url: str) -> str:
    """Point bare Postgres URLs at the psycopg 3 driver.

    Hosted providers hand out `postgres://` or `postgresql://` URLs, which
    SQLAlchemy would otherwise try to open with psycopg2.
    """
    authority = url.split("://", 1)[-1].split("/", 1)[0]
    if authority.count("@") > 1:
        raise ValueError(
            "the database URL contains more than one '@'. If the password contains "
            "'@', write it as '%40' (percent-encode special characters in passwords)."
        )
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def validate_schema_name(name: str) -> str:
    """Schema names are interpolated into `SET search_path`; allow only plain identifiers."""
    if not _SCHEMA_NAME.fullmatch(name):
        raise ValueError(
            f"invalid schema name {name!r}: use lowercase letters, digits and underscores"
        )
    return name


class Settings(BaseSettings):
    # hide_input_in_errors: validation errors would otherwise echo the raw
    # environment, connection string and password included.
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, extra="ignore", hide_input_in_errors=True
    )

    # repr=False: the URL carries the password and must never reach logs or
    # tracebacks (pytest prints fixture values when a test fails).
    database_url: Annotated[str, Field(repr=False, min_length=1)]
    environment: Environment
    # The Postgres schema the app's tables live in. The test suite overrides it
    # so tests can share a database with development without touching its data.
    database_schema: str = "public"
    # Unset means "on in development, off everywhere else"; see demo_routes_enabled.
    enable_demo_routes: bool | None = None
    min_voucher_cents: int = Field(default=1000, gt=0)
    max_voucher_cents: int = Field(default=500000, gt=0)

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        return normalize_database_url(value)

    @field_validator("database_schema")
    @classmethod
    def _validate_database_schema(cls, value: str) -> str:
        return validate_schema_name(value)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.environment == "production" and self.enable_demo_routes:
            raise ValueError(
                "ENABLE_DEMO_ROUTES must not be true in production: the demo "
                "routes vend vouchers and stand in for an issuer's terminal"
            )
        if self.min_voucher_cents > self.max_voucher_cents:
            raise ValueError("MIN_VOUCHER_CENTS must not exceed MAX_VOUCHER_CENTS")
        return self

    @property
    def demo_routes_enabled(self) -> bool:
        if self.enable_demo_routes is None:
            return self.environment == "development"
        return self.enable_demo_routes


@lru_cache
def get_settings() -> Settings:
    return Settings()
