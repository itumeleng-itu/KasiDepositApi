"""DEMO SCAFFOLDING — NOT A PRODUCTION SURFACE.

These endpoints stand in for a voucher issuer's vending terminal: the till
at the spaza shop that takes cash and prints a PIN. In the real product that
terminal belongs to the issuer, not to us.

- They must never be exposed to, or called by, the mobile app. The app only
  ever redeems; it can never vend.
- They must be disabled in any real deployment. With ENABLE_DEMO_ROUTES
  false the router is not mounted at all, and production refuses to start
  with it true (see app/config.py).
- Every response under /demo carries `X-Demo-Surface: true`, errors included.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.db import get_session
from app.schemas.voucher import VendRequest, VendResponse, VoucherListItem
from app.switch import AmountOutOfRange, VoucherSwitch

PREFIX = "/demo"
DEMO_HEADER = "X-Demo-Surface"
RECENT_LIMIT = 50

router = APIRouter(prefix=PREFIX, tags=["demo"])


def get_switch(request: Request) -> VoucherSwitch:
    switch: VoucherSwitch = request.app.state.switch
    return switch


SessionDep = Annotated[Session, Depends(get_session)]
SwitchDep = Annotated[VoucherSwitch, Depends(get_switch)]


@router.post("/vouchers", status_code=status.HTTP_201_CREATED)
def vend_voucher(body: VendRequest, session: SessionDep, switch: SwitchDep) -> VendResponse:
    try:
        vended = switch.vend(session, body.amount_cents)
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
    return VendResponse.from_vended(vended)


@router.get("/vouchers")
def list_recent_vouchers(session: SessionDep, switch: SwitchDep) -> list[VoucherListItem]:
    """The most recent vouchers, for the demo till screen. PINs are masked."""
    return [VoucherListItem.from_summary(s) for s in switch.recent(session, RECENT_LIMIT)]


def _is_demo_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def mount(app: FastAPI) -> None:
    """Mount the demo surface: the router plus the header on every /demo response.

    The header is set in middleware rather than on the router so that it also
    covers validation errors, which FastAPI raises before any route runs.
    """
    app.include_router(router)

    @app.middleware("http")
    async def demo_surface_header(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if _is_demo_path(request.url.path):
            response.headers[DEMO_HEADER] = "true"
        return response
