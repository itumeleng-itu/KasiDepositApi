"""Settings, read from the environment (and `.env` at the project root).

`DATABASE_URL` and `ENVIRONMENT` have no defaults: if either is missing the
app refuses to start. `ENABLE_DEMO_ROUTES` defaults to true only in
development. In production the demo routes may be switched on only together
with a `DEMO_API_KEY`: every /demo request must then present it, and without
it /demo answers 404 as if it did not exist (see app/routes/demo.py).

One database, two schemas. `DEV_SCHEMA` (default `public`) holds the app's
data in development and production; `TEST_SCHEMA` (default `kd_test`) holds
the test suite's. `ENVIRONMENT` picks which one a process uses (see
`Settings.active_schema`). The test suite refuses to run if the two are the same.
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
    dev_schema: str = "public"
    test_schema: str = "kd_test"
    # Unset means "on in development, off everywhere else"; see demo_routes_enabled.
    enable_demo_routes: bool | None = None
    min_voucher_cents: int = Field(default=1000, gt=0)
    max_voucher_cents: int = Field(default=500000, gt=0)
    # Our fee per deposit, in basis points (hundredths of a percent) of the
    # voucher amount, deducted from it: 250 is 2.5%. See money.percent_of.
    fee_basis_points: int = Field(default=250, ge=0, lt=10_000)
    # How long a voucher lookup's token may be used to create a deposit.
    voucher_token_ttl_seconds: int = Field(default=600, gt=0)
    # Secret that /demo requests must present in the X-Demo-Key header. Required
    # when the demo routes are on in production; optional elsewhere.
    demo_api_key: Annotated[str | None, Field(default=None, repr=False, min_length=32)]
    # Secret that SA ID numbers and bank account numbers are encrypted and
    # hashed under (app/pii.py). Unset: the API still serves, but refuses to
    # register users or take account payouts, so nothing personal is ever
    # stored unencrypted. Losing it makes stored ciphertexts unrecoverable.
    pii_key: Annotated[str | None, Field(default=None, repr=False, min_length=32)]
    # Rate limits on /v1 (app/rate_limit.py): N requests per window. Read at
    # startup, so changing one in the Render dashboard takes effect on the
    # restart that follows, with no code change or build.
    # ShapID directory reads, per client IP.
    rate_limit_shapid_requests: int = Field(default=10, gt=0)
    rate_limit_shapid_window_seconds: int = Field(default=60, gt=0)
    # Registrations, per client IP. Generous for one person, because mobile
    # carriers put many phones behind one address (carrier-grade NAT).
    rate_limit_register_requests: int = Field(default=20, gt=0)
    rate_limit_register_window_seconds: int = Field(default=3600, gt=0)
    # Voucher lookups, per access token: room for a few mistyped PINs.
    rate_limit_lookup_requests: int = Field(default=10, gt=0)
    rate_limit_lookup_window_seconds: int = Field(default=60, gt=0)
    # Deposit status reads, per access token. The app polls at most once a
    # second (src/polling.ts), 60 a minute, plus one each time it returns to
    # the foreground; 120 leaves double that.
    rate_limit_deposit_status_requests: int = Field(default=120, gt=0)
    rate_limit_deposit_status_window_seconds: int = Field(default=60, gt=0)
    # Deposit risk (app/risk.py). Off: every deposit is allowed unscored, a
    # kill switch for a misbehaving rule that needs no deploy.
    risk_enabled: bool = True
    # Each signal that fires adds its weight to
    # the score; the score picks the band. Defaults: one strong signal warns
    # (the app asks the user to confirm), two together refuse.
    risk_warn_score: int = Field(default=30, gt=0)
    risk_review_score: int = Field(default=60, gt=0)
    # How far back a user's own pattern is read.
    risk_history_days: int = Field(default=30, ge=2, le=365)
    # Velocity: deposits in the last hour (this one included) above this fires.
    risk_velocity_hour_max: int = Field(default=3, gt=0)
    # ...and in the last 24 hours, above the larger of this floor and this
    # multiple of the user's own daily average over the rest of the history.
    risk_velocity_day_floor: int = Field(default=5, gt=0)
    risk_velocity_day_multiplier: float = Field(default=3.0, gt=1)
    risk_weight_velocity_hour: int = Field(default=40, ge=0)
    risk_weight_velocity_day: int = Field(default=30, ge=0)
    # Novelty: a payout method first used within this many minutes of being
    # added, by a user who has deposited elsewhere before.
    risk_novelty_minutes: int = Field(default=10, gt=0)
    risk_weight_novelty: int = Field(default=30, ge=0)
    # Value: this many times the user's median deposit, once they have at
    # least `risk_value_min_history` deposits to compare against.
    risk_value_multiplier: float = Field(default=4.0, gt=1)
    risk_value_min_history: int = Field(default=3, gt=0)
    risk_weight_value: int = Field(default=30, ge=0)
    # Redemption lag, from vend to deposit. Short is weak evidence (honest
    # users often redeem at the till); long suggests a PIN that changed hands.
    risk_lag_short_seconds: int = Field(default=30, ge=0)
    risk_lag_long_hours: int = Field(default=72, gt=0)
    risk_weight_lag_short: int = Field(default=10, ge=0)
    risk_weight_lag_long: int = Field(default=30, ge=0)
    # Destination churn: this many different destinations (this one
    # included) within the window.
    risk_churn_window_hours: int = Field(default=24, gt=0)
    risk_churn_destinations: int = Field(default=3, ge=2)
    risk_weight_churn: int = Field(default=40, ge=0)
    # How many forced-band minutes the /demo/risk switch may ask for.
    risk_demo_force_max_minutes: int = Field(default=60, gt=0)
    # The payout worker (app/worker.py): advances deposits nobody is polling.
    # Off by default, so development and the test suite are untouched.
    payout_worker_enabled: bool = False
    # How often it looks for in-flight deposits.
    payout_worker_interval_seconds: float = Field(default=15.0, gt=0)
    # Leave a deposit alone this long after its last change: the app is
    # usually polling it then, and would advance it anyway.
    payout_worker_min_age_seconds: int = Field(default=10, ge=0)
    # At most this many deposits per pass.
    payout_worker_batch_size: int = Field(default=50, gt=0, le=1000)
    # How many proxies in front of us append to X-Forwarded-For: 1 on Render.
    # 0 ignores the header and uses the socket peer (no proxy, e.g. locally).
    trusted_proxy_hops: int = Field(default=1, ge=0, le=5)

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        return normalize_database_url(value)

    @field_validator("dev_schema", "test_schema")
    @classmethod
    def _validate_schema(cls, value: str) -> str:
        return validate_schema_name(value)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.environment == "production" and self.enable_demo_routes and not self.demo_api_key:
            raise ValueError(
                "ENABLE_DEMO_ROUTES in production needs DEMO_API_KEY: without it the "
                "demo routes would let anyone vend vouchers and spend the float"
            )
        if self.min_voucher_cents > self.max_voucher_cents:
            raise ValueError("MIN_VOUCHER_CENTS must not exceed MAX_VOUCHER_CENTS")
        if self.risk_warn_score >= self.risk_review_score:
            raise ValueError("RISK_WARN_SCORE must be below RISK_REVIEW_SCORE")
        return self

    @property
    def active_schema(self) -> str:
        """The schema this process works in: TEST_SCHEMA under test, else DEV_SCHEMA."""
        return self.test_schema if self.environment == "test" else self.dev_schema

    @property
    def demo_routes_enabled(self) -> bool:
        if self.enable_demo_routes is None:
            return self.environment == "development"
        return self.enable_demo_routes


@lru_cache
def get_settings() -> Settings:
    return Settings()
