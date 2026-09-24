"""Unauthenticated liveness check. Reports nothing beyond ok/unreachable.

Two limits keep it from ever hanging:

- An unreachable database fails at the connect timeout (10s): the driver
  gives up opening the connection, and the check answers 503.
- A connection that opens but then stalls (the network drops mid-query) is
  cut off by a hard deadline on the whole check. That deadline must leave
  room for a normal cold check, which opens a connection and then runs the
  session setup and a query: on a slow link that alone takes ~7s. So the
  deadline is the connect timeout plus a query budget, never the connect
  timeout itself (that made a healthy but slow database report unreachable).

When the deadline passes, the worker thread is left to fail on its own (the
connection's timeouts and keepalives end it) and the 503 goes out at once.
"""

import asyncio

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from app.db import CONNECT_TIMEOUT_SECONDS

router = APIRouter(tags=["health"])

QUERY_BUDGET_SECONDS = 5
HEALTH_TIMEOUT_SECONDS: float = CONNECT_TIMEOUT_SECONDS + QUERY_BUDGET_SECONDS

OK = {"status": "ok", "database": "ok"}
UNREACHABLE = {"status": "error", "database": "unreachable"}


def ping_database(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        session.execute(text("SELECT 1"))


@router.get("/health")
async def health(request: Request, response: Response) -> dict[str, str]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    try:
        await asyncio.wait_for(
            run_in_threadpool(ping_database, factory), timeout=HEALTH_TIMEOUT_SECONDS
        )
    except (SQLAlchemyError, TimeoutError):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return UNREACHABLE
    return OK
