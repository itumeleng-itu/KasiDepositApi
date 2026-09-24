"""The mobile app's API. The real surface: no X-Demo-Surface header here.

Matches the app's src/api/wire.ts exactly (the app is the source of truth):

    POST /v1/vouchers/lookup   {pin}                          -> voucher lookup
    GET  /v1/shapid/{shap_id}  (URL-encoded: + and @ survive) -> resolved ShapID
    POST /v1/deposits          {voucher_token, destination}   -> deposit
                               Idempotency-Key header required
    GET  /v1/deposits/{id}                                    -> deposit

No endpoint here accepts an amount: the backend reads it from the voucher row.

Every refusal is `{"reason": "<string>"}`. The strings are exactly the app's
FailureReason values, because the app maps them to what the user reads; the
HTTP status for each is in REASON_STATUS.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.banks import BANK_API_CODES
from app.db import get_session
from app.deposits import DepositService, Refused
from app.schemas.v1 import (
    CreateDepositRequest,
    DepositResponse,
    LookupRequest,
    LookupResponse,
    ShapIdResponse,
)
from app.shapid import ShapIdError, resolve

router = APIRouter(prefix="/v1", tags=["v1"])

REASON_STATUS: dict[str, int] = {
    "voucher_not_found": status.HTTP_404_NOT_FOUND,
    "voucher_already_redeemed": status.HTTP_409_CONFLICT,
    "voucher_too_small": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "insufficient_float": status.HTTP_409_CONFLICT,
    "shapid_not_found": status.HTTP_404_NOT_FOUND,
    "shapid_suspended": status.HTTP_409_CONFLICT,
    "shapid_ambiguous": status.HTTP_409_CONFLICT,
    "shapid_invalid_format": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "invalid_destination": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "idempotency_key_required": status.HTTP_400_BAD_REQUEST,
    "idempotency_key_invalid": status.HTTP_400_BAD_REQUEST,
    "deposit_not_found": status.HTTP_404_NOT_FOUND,
}


def get_deposits(request: Request) -> DepositService:
    service: DepositService = request.app.state.deposits
    return service


SessionDep = Annotated[Session, Depends(get_session)]
DepositsDep = Annotated[DepositService, Depends(get_deposits)]


@router.post("/vouchers/lookup")
def lookup_voucher(body: LookupRequest, session: SessionDep, deposits: DepositsDep) -> LookupResponse:
    return LookupResponse.of(deposits.lookup_voucher(session, body.pin))


@router.get("/shapid/{shap_id}")
def resolve_shap_id(shap_id: str) -> ShapIdResponse:
    try:
        resolved = resolve(shap_id)
    except ShapIdError as exc:
        raise Refused(exc.reason) from None
    return ShapIdResponse(shap_name=resolved.shap_name, bank=BANK_API_CODES[resolved.bank_id])


@router.post("/deposits", status_code=status.HTTP_201_CREATED)
def create_deposit(
    body: CreateDepositRequest,
    response: Response,
    session: SessionDep,
    deposits: DepositsDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> DepositResponse:
    if not idempotency_key:
        raise Refused("idempotency_key_required")
    try:
        uuid.UUID(idempotency_key)
    except ValueError:
        raise Refused("idempotency_key_invalid") from None
    view, created = deposits.create_deposit(
        session, body.voucher_token, body.destination, idempotency_key
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return DepositResponse.of(view)


@router.get("/deposits/{deposit_id}")
def get_deposit(deposit_id: str, session: SessionDep, deposits: DepositsDep) -> DepositResponse:
    try:
        parsed = uuid.UUID(deposit_id)
    except ValueError:
        raise Refused("deposit_not_found") from None
    return DepositResponse.of(deposits.get_deposit(session, parsed))


async def _refused(_request: Request, exc: Exception) -> JSONResponse:
    reason = exc.reason if isinstance(exc, Refused) else "unknown"
    return JSONResponse({"reason": reason}, status_code=REASON_STATUS.get(reason, 400))


def mount(app: FastAPI) -> None:
    app.include_router(router)
    app.add_exception_handler(Refused, _refused)
