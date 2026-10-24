"""DEMO SCAFFOLDING: stands in for a voucher issuer's vending terminal.

These endpoints stand in for the till at the spaza shop that takes cash and
prints a PIN. In the real product that terminal belongs to the issuer, not to
us. They also let the demo operator top up and inspect the float, keep the
demo PayShap directory (/demo/shapids): the numbers that resolve to a real
name and bank in the demo, since we have no connection to PayShap itself, and
force a deposit risk band for a few minutes (/demo/risk), so the warn and
review behaviour can be shown without building up a real pattern.

- They must never be exposed to, or called by, the mobile app. The app only
  ever redeems; it can never vend.
- Off unless switched on. With ENABLE_DEMO_ROUTES false the router is not
  mounted at all. In production they may be on only with a DEMO_API_KEY
  (app/config.py).
- With a DEMO_API_KEY set, every /demo request must carry it in the
  X-Demo-Key header. Without it (or with the wrong key) the answer is a plain
  404, before routing or body validation, so a stranger cannot tell /demo
  exists, and /demo is left out of the OpenAPI schema.
- Every authorised response under /demo carries `X-Demo-Surface: true`,
  errors included.
"""

import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_session
from app.ledger import available_float, balance_of, fund_float
from app.models import DemoShapId, LedgerAccount
from app.schemas.demo_shapid import AddDemoShapIdRequest, DemoShapIdResponse, normalise_number
from app.risk import Band, RiskOverride
from app.schemas.demo_risk import ForcedRiskResponse, ForceRiskRequest
from app.schemas.float import FloatResponse, FundFloatRequest
from app.schemas.voucher import VendRequest, VendResponse, VoucherListItem
from app.deposits import DepositService
from app.switch import AmountOutOfRange, VoucherSwitch

PREFIX = "/demo"
DEMO_HEADER = "X-Demo-Surface"
KEY_HEADER = "X-Demo-Key"
RECENT_LIMIT = 50

router = APIRouter(prefix=PREFIX, tags=["demo"])


def get_switch(request: Request) -> VoucherSwitch:
    switch: VoucherSwitch = request.app.state.switch
    return switch


SessionDep = Annotated[Session, Depends(get_session)]
SwitchDep = Annotated[VoucherSwitch, Depends(get_switch)]


def get_deposits(request: Request) -> DepositService:
    service: DepositService = request.app.state.deposits
    return service


DepositsDep = Annotated[DepositService, Depends(get_deposits)]


@router.post("/vouchers", status_code=status.HTTP_201_CREATED)
def vend_voucher(
    body: VendRequest,
    response: Response,
    session: SessionDep,
    switch: SwitchDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> VendResponse:
    """Vend one voucher. `Idempotency-Key` is required, as on /v1/deposits.

    Required rather than optional because the cash is already in the till by
    the time this is called: a client that retries without a key buys the
    shop a second voucher, and whether that happens must not be left to the
    client remembering. A UUID, so one till cannot guess another's keys.
    """
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=[{"type": "missing", "loc": ["header", "Idempotency-Key"], "msg": "required"}],
        )
    try:
        uuid.UUID(idempotency_key)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=[
                {
                    "type": "uuid_parsing",
                    "loc": ["header", "Idempotency-Key"],
                    "msg": "must be a UUID",
                }
            ],
        ) from None
    try:
        vended = switch.vend(session, body.amount_cents, idempotency_key)
    except AmountOutOfRange as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[
                {
                    "type": "out_of_range",
                    "loc": ["body", "amount_cents"],
                    "msg": str(exc),
                    "input": exc.amount_cents,
                    "ctx": {"min": exc.min_cents, "max": exc.max_cents},
                }
            ],
        ) from exc
    session.commit()
    # 200 rather than 201 on a replay: nothing was created by this call.
    if vended.replayed:
        response.status_code = status.HTTP_200_OK
    return VendResponse.from_vended(vended)


@router.get("/vouchers")
def list_recent_vouchers(
    session: SessionDep, switch: SwitchDep, deposits: DepositsDep
) -> list[VoucherListItem]:
    """The most recent vouchers, for the demo till screen. PINs are masked.

    Each redeemed voucher carries what became of its deposit, so a cashier
    can answer "I redeemed it and the money never came" instead of only
    "it says used". The trace is deliberately thin — reference, outcome,
    time — and never says where the money was going.
    """
    summaries = switch.recent(session, RECENT_LIMIT)
    traces = deposits.traces_for_vouchers(session, [s.serial for s in summaries])
    return [VoucherListItem.from_summary(s, traces.get(s.serial)) for s in summaries]


def _float(session: Session) -> FloatResponse:
    return FloatResponse.of(
        settlement_cents=balance_of(session, LedgerAccount.SETTLEMENT),
        available_cents=available_float(session),
    )


@router.get("/float")
def get_float(session: SessionDep) -> FloatResponse:
    """The float: what is in the settlement account, and what is still free to pay out."""
    response = _float(session)
    session.rollback()
    return response


@router.post("/float")
def top_up_float(body: FundFloatRequest, session: SessionDep) -> FloatResponse:
    """Top up the float from capital (settlement +X, capital -X in the ledger)."""
    fund_float(session, body.amount_cents)
    session.commit()
    return _float(session)


@router.get("/shapids")
def list_demo_shapids(session: SessionDep) -> list[DemoShapIdResponse]:
    """The demo PayShap directory, newest first."""
    entries = session.scalars(select(DemoShapId).order_by(DemoShapId.created_at.desc())).all()
    response = [DemoShapIdResponse.of(e) for e in entries]
    session.rollback()
    return response


@router.post("/shapids", status_code=status.HTTP_201_CREATED)
def add_demo_shapid(body: AddDemoShapIdRequest, session: SessionDep) -> DemoShapIdResponse:
    """List a number (or replace its entry): it now resolves to this person and bank."""
    entry = session.get(DemoShapId, body.number)
    if entry is None:
        entry = DemoShapId(number=body.number, shap_name=body.full_name, bank_id=body.bank)
        session.add(entry)
    else:
        entry.shap_name, entry.bank_id = body.full_name, body.bank
    session.commit()
    return DemoShapIdResponse.of(entry)


@router.delete("/shapids/{number}", status_code=status.HTTP_204_NO_CONTENT)
def remove_demo_shapid(number: str, session: SessionDep) -> Response:
    try:
        key = normalise_number(number)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    entry = session.get(DemoShapId, key)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    session.delete(entry)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _override(request: Request) -> RiskOverride:
    override: RiskOverride = request.app.state.risk_override
    return override


def _forced(override: RiskOverride) -> ForcedRiskResponse:
    current = override.current(datetime.now(UTC))
    if current is None:
        return ForcedRiskResponse(band=None, expires_at=None)
    band, until = current
    return ForcedRiskResponse(band=band.value, expires_at=until)  # type: ignore[arg-type]


@router.get("/risk")
def get_forced_risk(request: Request) -> ForcedRiskResponse:
    """The forced risk band, if one is on."""
    return _forced(_override(request))


@router.post("/risk")
def force_risk(body: ForceRiskRequest, request: Request) -> ForcedRiskResponse:
    """Every deposit gets this band until it expires or is cleared. In
    process memory: it applies to the one instance that served this call."""
    settings: Settings = request.app.state.settings
    if body.minutes > settings.risk_demo_force_max_minutes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"minutes must be at most {settings.risk_demo_force_max_minutes}",
        )
    override = _override(request)
    override.force(Band(body.band), datetime.now(UTC) + timedelta(minutes=body.minutes))
    return _forced(override)


@router.delete("/risk", status_code=status.HTTP_204_NO_CONTENT)
def clear_forced_risk(request: Request) -> Response:
    """Back to real scoring."""
    _override(request).clear()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _is_demo_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def _authorised(request: Request, key: str | None) -> bool:
    if key is None:
        return True
    presented = request.headers.get(KEY_HEADER, "")
    return secrets.compare_digest(presented.encode(), key.encode())


def mount(app: FastAPI, settings: Settings) -> None:
    """Mount the demo surface: the key check, the router, and the demo header.

    Both run in middleware rather than on the router, so they also cover
    requests FastAPI rejects before any route runs (malformed bodies).
    """
    key = settings.demo_api_key
    app.include_router(router, include_in_schema=key is None)

    @app.middleware("http")
    async def demo_surface(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not _is_demo_path(request.url.path):
            return await call_next(request)
        if not _authorised(request, key):
            # Indistinguishable from a path that does not exist.
            return JSONResponse({"detail": "Not Found"}, status_code=status.HTTP_404_NOT_FOUND)
        response = await call_next(request)
        response.headers[DEMO_HEADER] = "true"
        return response
