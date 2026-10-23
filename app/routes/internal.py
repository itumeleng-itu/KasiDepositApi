"""POST /internal/sweep: finish deposits nobody is polling for.

Why this exists at all: from `charged` onwards a deposit's voucher is
already redeemed, so the customer's cash is spent. The only thing that
moved a deposit on was the depositor reading its status, and
`get_deposit` serves a deposit only to the user who made it — so a phone
that went away left its deposit stuck, the money neither arrived nor
returned, with no request in the system able to rescue it. See
app/deposits.py `sweep`.

Nothing in this process runs on a timer. A thread would die with the
instance (Render free sleeps after 15 minutes without traffic), so the
sweep is a request, and whatever schedules it lives outside: a Render
cron job, or any pinger. The same call keeps the instance warm, which
the free plan needs before a demo anyway.

Not part of the app's API and not in the OpenAPI schema. Mounted only
when SWEEP_KEY is set, and then every request must carry it in the
X-Sweep-Key header; without it the answer is a plain 404, as under /demo,
so a stranger cannot tell the route is there. The key matters less for
secrecy than for cost: a sweep talks to the payout provider, and an open
endpoint would let anyone make us do that as fast as they liked.

Safe to call at any time and as often as anything likes. It only ever
advances deposits that were already due to advance, through the same
code path a status poll uses, and every step re-checks under the
deposit's row lock.
"""

import secrets
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_session
from app.deposits import SWEEP_LIMIT, SWEEP_STALE_AFTER, DepositService

PREFIX = "/internal"
KEY_HEADER = "X-Sweep-Key"

router = APIRouter(prefix=PREFIX, include_in_schema=False)


def get_deposits(request: Request) -> DepositService:
    service: DepositService = request.app.state.deposits
    return service


SessionDep = Annotated[Session, Depends(get_session)]
DepositsDep = Annotated[DepositService, Depends(get_deposits)]


class SweepResponse(BaseModel):
    """Counts, never deposit ids or destinations: this answer goes to
    whatever is scheduling the call, which has no business seeing either."""

    examined: int
    settled: int
    failed: int
    unresolved: int
    skipped: int
    # True when the call filled its budget, so there is probably more
    # waiting and the scheduler may want to come back sooner.
    more_likely: bool


@router.post("/sweep")
def sweep(
    session: SessionDep,
    deposits: DepositsDep,
    stale_after_seconds: Annotated[int, Query(ge=1, le=86_400)] = int(
        SWEEP_STALE_AFTER.total_seconds()
    ),
    limit: Annotated[int, Query(ge=1, le=200)] = SWEEP_LIMIT,
) -> SweepResponse:
    result = deposits.sweep(
        session, stale_after=timedelta(seconds=stale_after_seconds), limit=limit
    )
    return SweepResponse(
        examined=result.examined,
        settled=result.settled,
        failed=result.failed,
        unresolved=result.unresolved,
        skipped=result.skipped,
        more_likely=result.examined == limit,
    )


def _is_internal_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def _authorised(request: Request, key: str) -> bool:
    # compare_digest, not ==: a plain comparison leaks the key's prefix
    # through how long it takes to fail.
    presented = request.headers.get(KEY_HEADER, "")
    return secrets.compare_digest(presented.encode(), key.encode())


def mount(app: FastAPI, settings: Settings) -> None:
    """Mount the sweep route behind its key.

    Does nothing without a key: an unauthenticated sweep endpoint would let
    anyone drive our payout provider, so no key means no route.

    The check is middleware rather than a dependency so that it also covers
    requests FastAPI rejects before any route runs, and so the 404 is
    byte-identical to a real one.
    """
    key = settings.sweep_key
    if key is None:
        return
    app.include_router(router)

    @app.middleware("http")
    async def sweep_key_required(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if _is_internal_path(request.url.path) and not _authorised(request, key):
            return JSONResponse(
                {"detail": "Not Found"}, status_code=status.HTTP_404_NOT_FOUND
            )
        return await call_next(request)
