"""Application factory.

Run with:  uvicorn app.main:create_app --factory

A factory rather than a module-level `app` so that importing this module
never reads the environment; settings are read when the server starts, and a
missing `DATABASE_URL` fails the startup loudly. Startup also refuses a
database schema that is behind the code (app/schema_check.py).
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI

from app import log_redaction
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.account_verification import AccountVerifier, MockAccountVerifier
from app.deposits import DepositService
from app.identity import IdentityVerifier, MockIdentityVerifier
from app.payouts import MockPayoutProvider, PayoutProvider
from app.payout_methods import PayoutMethodService
from app.pii import PiiCipher
from app.rate_limit import Limits, RateLimiter
from app.risk import RiskOverride, RiskThresholds, RuleBasedRisk
from app.routes import demo, health, till, v1
from app.schema_check import check_schema_is_current
from app.switch import VoucherSwitch
from app.users import UserService
from app.worker import PayoutWorker

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    payout_provider: PayoutProvider | None = None,
    identity_verifier: IdentityVerifier | None = None,
    account_verifier: AccountVerifier | None = None,
) -> FastAPI:
    """The provider and verifiers default to the mocks; tests pass their own."""
    settings = settings or get_settings()
    engine = make_engine(settings.database_url, settings.active_schema)
    log_redaction.install()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        worker: PayoutWorker | None = app.state.payout_worker
        try:
            check_schema_is_current(engine)
            if worker is not None:
                worker.start()
            yield
        finally:
            if worker is not None:
                worker.stop()
            engine.dispose()

    # No interactive docs or schema in production: nothing to map the API for a stranger.
    public_docs = settings.environment != "production"
    app = FastAPI(
        title="KasiDeposit API",
        lifespan=lifespan,
        docs_url="/docs" if public_docs else None,
        redoc_url="/redoc" if public_docs else None,
        openapi_url="/openapi.json" if public_docs else None,
    )
    app.state.settings = settings
    app.state.session_factory = make_session_factory(engine)
    # No PII_KEY: registration and account payouts are refused (app/pii.py).
    pii = PiiCipher(settings.pii_key) if settings.pii_key else None
    switch = VoucherSwitch(settings.min_voucher_cents, settings.max_voucher_cents)
    app.state.switch = switch
    # The /demo/risk switch writes here; in process memory (app/risk.py).
    app.state.risk_override = RiskOverride()
    app.state.deposits = DepositService(
        switch=switch,
        provider=payout_provider or MockPayoutProvider(),
        fee_basis_points=settings.fee_basis_points,
        min_voucher_cents=settings.min_voucher_cents,
        token_ttl=timedelta(seconds=settings.voucher_token_ttl_seconds),
        pii=pii,
        risk=RuleBasedRisk(RiskThresholds.from_settings(settings)) if settings.risk_enabled else None,
        risk_override=app.state.risk_override,
        risk_history_days=settings.risk_history_days,
    )
    app.state.users = UserService(pii=pii, verifier=identity_verifier or MockIdentityVerifier())
    app.state.payout_methods = PayoutMethodService(
        pii=pii, account_verifier=account_verifier or MockAccountVerifier()
    )
    # Off unless PAYOUT_WORKER_ENABLED: advances deposits nobody polls (app/worker.py).
    app.state.payout_worker = (
        PayoutWorker(
            app.state.session_factory,
            app.state.deposits,
            interval_seconds=settings.payout_worker_interval_seconds,
            min_age_seconds=settings.payout_worker_min_age_seconds,
            batch_size=settings.payout_worker_batch_size,
        )
        if settings.payout_worker_enabled
        else None
    )

    # In process memory: right for one worker only (see app/rate_limit.py).
    app.state.rate_limiter = RateLimiter(Limits.from_settings(settings), settings.trusted_proxy_hops)
    if _worker_count() > 1:
        logger.warning(
            "WEB_CONCURRENCY=%d: rate limits are kept per process, so each client gets "
            "that many times the configured limit. Move them to a shared store first.",
            _worker_count(),
        )

    app.include_router(health.router)
    v1.mount(app)
    if settings.demo_routes_enabled:
        # Absent, not forbidden: with the flag off, /demo simply does not exist.
        demo.mount(app, settings)
        till.mount(app)
    return app


def _worker_count() -> int:
    """Uvicorn's worker count when it comes from WEB_CONCURRENCY (its env default)."""
    try:
        return int(os.environ.get("WEB_CONCURRENCY", "1"))
    except ValueError:
        return 1
