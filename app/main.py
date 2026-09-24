"""Application factory.

Run with:  uvicorn app.main:create_app --factory

A factory rather than a module-level `app` so that importing this module
never reads the environment; settings are read when the server starts, and a
missing `DATABASE_URL` fails the startup loudly. Startup also refuses a
database schema that is behind the code (app/schema_check.py).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI

from app import log_redaction
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.deposits import DepositService
from app.payouts import MockPayoutProvider, PayoutProvider
from app.routes import demo, health, v1
from app.schema_check import check_schema_is_current
from app.switch import VoucherSwitch


def create_app(
    settings: Settings | None = None, payout_provider: PayoutProvider | None = None
) -> FastAPI:
    """`payout_provider` defaults to the mock; tests pass one on a fake clock."""
    settings = settings or get_settings()
    engine = make_engine(settings.database_url, settings.active_schema)
    log_redaction.install()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            check_schema_is_current(engine)
            yield
        finally:
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
    switch = VoucherSwitch(settings.min_voucher_cents, settings.max_voucher_cents)
    app.state.switch = switch
    app.state.deposits = DepositService(
        switch=switch,
        provider=payout_provider or MockPayoutProvider(),
        fee_cents=settings.fee_cents,
        min_voucher_cents=settings.min_voucher_cents,
        token_ttl=timedelta(seconds=settings.voucher_token_ttl_seconds),
    )

    app.include_router(health.router)
    v1.mount(app)
    if settings.demo_routes_enabled:
        # Absent, not forbidden: with the flag off, /demo simply does not exist.
        demo.mount(app, settings)
    return app
