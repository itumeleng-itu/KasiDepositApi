"""The mobile app's API. The real surface: no X-Demo-Surface header here.

Matches the app's src/api/wire.ts exactly (the app is the source of truth):

    POST /v1/vouchers/lookup   {pin}                          -> voucher lookup
    GET  /v1/shapid/{shap_id}  (URL-encoded: + and @ survive) -> resolved ShapID
    POST /v1/deposits          {voucher_token, payout_method_id | destination,
                                risk_acknowledged?}           -> deposit
                               Idempotency-Key header required
    GET  /v1/deposits/{id}                                    -> deposit
    POST /v1/users             {full_names, id_number}        -> registered user
    GET  /v1/me/deposits       ?limit=1..50 (default 20)      -> {deposits: [...]}
    GET  /v1/me/payout-methods                                -> {payout_methods: [...]}
    POST /v1/me/payout-methods {kind: shap_id, shap_id} | {kind: account, bank, account_number}
                               (+ make_default)               -> the method, 201 (200 if already saved)
    POST /v1/me/payout-methods/{id}/default                   -> {payout_methods: [...]}
    DELETE /v1/me/payout-methods/{id}                         -> {payout_methods: [...]}

Every route except POST /v1/users needs `Authorization: Bearer <access_token>`;
without a valid one it is 401 {"reason": "not_registered"} (app/users.py). A
deposit is visible only to the user who made it.

Four routes are rate limited (app/rate_limit.py): the ShapID read and
registration per client IP, voucher lookup and deposit status per access
token. Over the limit is 429 {"reason": "rate_limited"} with Retry-After, and
no remaining-quota headers anywhere.

No endpoint here accepts an amount: the backend reads it from the voucher row.

A deposit is risk-assessed before anything is charged (app/risk.py). Two
refusals come of it, and neither says why: 409 confirmation_required (the app
shows its interstitial, then resends with the same Idempotency-Key and
"risk_acknowledged": true) and 403 deposit_declined.

Every refusal is `{"reason": "<string>"}`. The strings are exactly the app's
FailureReason values, because the app maps them to what the user reads; the
HTTP status for each is in REASON_STATUS.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.banks import BANK_API_CODES
from app.db import get_session
from app.deposits import DepositService, Refused
from app.schemas.v1 import (
    CreateDepositRequest,
    DepositHistoryResponse,
    DepositRecordResponse,
    DepositResponse,
    LookupRequest,
    LookupResponse,
    RegisterRequest,
    RegisterResponse,
    ShapIdResponse,
    AddPayoutMethodRequest,
    PayoutMethodListResponse,
    PayoutMethodResponse,
)
from app.payout_methods import PayoutMethodService
from app.risk import CONFIRMATION_REQUIRED, DEPOSIT_DECLINED
from app.rate_limit import RateLimited, RateLimiter, client_ip, ip_key, token_key
from app.shapid import ShapIdError, demo_directory, resolve
from app.users import UserService

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
    "not_registered": status.HTTP_401_UNAUTHORIZED,
    "id_number_invalid": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "id_number_under_age": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "invalid_registration": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "id_verification_failed": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "id_number_already_registered": status.HTTP_409_CONFLICT,
    "shapid_name_mismatch": status.HTTP_409_CONFLICT,
    "registration_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "accounts_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "invalid_account": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "account_not_found": status.HTTP_404_NOT_FOUND,
    "account_holder_mismatch": status.HTTP_409_CONFLICT,
    "payout_method_limit": status.HTTP_409_CONFLICT,
    "payout_method_not_found": status.HTTP_404_NOT_FOUND,
    "rate_limited": status.HTTP_429_TOO_MANY_REQUESTS,
    CONFIRMATION_REQUIRED: status.HTTP_409_CONFLICT,
    DEPOSIT_DECLINED: status.HTTP_403_FORBIDDEN,
}


def get_deposits(request: Request) -> DepositService:
    service: DepositService = request.app.state.deposits
    return service


def get_users(request: Request) -> UserService:
    service: UserService = request.app.state.users
    return service


def get_payout_methods(request: Request) -> PayoutMethodService:
    service: PayoutMethodService = request.app.state.payout_methods
    return service


SessionDep = Annotated[Session, Depends(get_session)]
DepositsDep = Annotated[DepositService, Depends(get_deposits)]
UsersDep = Annotated[UserService, Depends(get_users)]
MethodsDep = Annotated[PayoutMethodService, Depends(get_payout_methods)]


def current_user(
    session: SessionDep,
    users: UsersDep,
    authorization: Annotated[str | None, Header()] = None,
) -> uuid.UUID:
    return users.authenticate(session, authorization)


UserIdDep = Annotated[uuid.UUID, Depends(current_user)]


def get_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.rate_limiter
    return limiter


LimiterDep = Annotated[RateLimiter, Depends(get_limiter)]


# Per-IP limits run before authentication (they sit in the route's
# `dependencies`, which FastAPI solves first), so they hold with or without a token.
def limit_shapid_by_ip(request: Request, limiter: LimiterDep) -> None:
    ip = client_ip(request, limiter.trusted_proxy_hops)
    limiter.hit(ip_key("shapid", ip), limiter.limits.shapid)


def limit_register_by_ip(request: Request, limiter: LimiterDep) -> None:
    ip = client_ip(request, limiter.trusted_proxy_hops)
    limiter.hit(ip_key("register", ip), limiter.limits.register)


# Per-token limits depend on current_user, so they run only after the token
# has been accepted: a made-up token is a 401 and never becomes a key.
def limit_lookup_by_token(
    _user_id: UserIdDep,
    limiter: LimiterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    limiter.hit(token_key("lookup", authorization), limiter.limits.lookup)


def limit_deposit_status_by_token(
    _user_id: UserIdDep,
    limiter: LimiterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    limiter.hit(token_key("deposit_status", authorization), limiter.limits.deposit_status)


@router.post(
    "/users", status_code=status.HTTP_201_CREATED, dependencies=[Depends(limit_register_by_ip)]
)
def register_user(
    body: RegisterRequest, response: Response, session: SessionDep, users: UsersDep
) -> RegisterResponse:
    registered = users.register(session, body.full_names, body.id_number)
    response.status_code = status.HTTP_201_CREATED if registered.created else status.HTTP_200_OK
    return RegisterResponse(
        user_id=registered.user_id,
        access_token=registered.access_token,
        full_names=registered.full_names,
    )


def _method_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise Refused("payout_method_not_found") from None


def _method_list(session: Session, methods: PayoutMethodService, user_id: uuid.UUID) -> PayoutMethodListResponse:
    return PayoutMethodListResponse(
        payout_methods=[PayoutMethodResponse.of(m) for m in methods.list(session, user_id)]
    )


@router.get("/me/payout-methods")
def list_payout_methods(
    session: SessionDep, methods: MethodsDep, user_id: UserIdDep
) -> PayoutMethodListResponse:
    return _method_list(session, methods, user_id)


@router.post("/me/payout-methods", status_code=status.HTTP_201_CREATED)
def add_payout_method(
    body: AddPayoutMethodRequest,
    response: Response,
    session: SessionDep,
    methods: MethodsDep,
    user_id: UserIdDep,
) -> PayoutMethodResponse:
    before = {m.id for m in methods.list(session, user_id)}
    if body.kind == "shap_id" and body.shap_id is not None:
        view = methods.add_shap_id(session, user_id, body.shap_id, body.make_default)
    elif body.kind == "account" and body.bank is not None and body.account_number is not None:
        view = methods.add_account(session, user_id, body.bank, body.account_number, body.make_default)
    else:
        raise Refused("invalid_destination")
    response.status_code = status.HTTP_200_OK if view.id in before else status.HTTP_201_CREATED
    return PayoutMethodResponse.of(view)


@router.post("/me/payout-methods/{method_id}/default")
def set_default_payout_method(
    method_id: str, session: SessionDep, methods: MethodsDep, user_id: UserIdDep
) -> PayoutMethodListResponse:
    methods.set_default(session, user_id, _method_id(method_id))
    return _method_list(session, methods, user_id)


@router.delete("/me/payout-methods/{method_id}")
def remove_payout_method(
    method_id: str, session: SessionDep, methods: MethodsDep, user_id: UserIdDep
) -> PayoutMethodListResponse:
    methods.remove(session, user_id, _method_id(method_id))
    return _method_list(session, methods, user_id)


@router.get("/me/deposits")
def list_my_deposits(
    session: SessionDep,
    deposits: DepositsDep,
    user_id: UserIdDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> DepositHistoryResponse:
    records = deposits.list_deposits(session, user_id, limit)
    return DepositHistoryResponse(deposits=[DepositRecordResponse.of(r) for r in records])


@router.post("/vouchers/lookup", dependencies=[Depends(limit_lookup_by_token)])
def lookup_voucher(
    body: LookupRequest, session: SessionDep, deposits: DepositsDep, _user_id: UserIdDep
) -> LookupResponse:
    return LookupResponse.of(deposits.lookup_voucher(session, body.pin))


@router.get("/shapid/{shap_id}", dependencies=[Depends(limit_shapid_by_ip)])
def resolve_shap_id(shap_id: str, session: SessionDep, _user_id: UserIdDep) -> ShapIdResponse:
    try:
        resolved = resolve(shap_id, demo_directory(session))
    except ShapIdError as exc:
        raise Refused(exc.reason) from None
    finally:
        session.rollback()  # the directory read; never leave it idle
    return ShapIdResponse(shap_name=resolved.shap_name, bank=BANK_API_CODES[resolved.bank_id])


@router.post("/deposits", status_code=status.HTTP_201_CREATED)
def create_deposit(
    body: CreateDepositRequest,
    response: Response,
    session: SessionDep,
    deposits: DepositsDep,
    user_id: UserIdDep,
    methods: MethodsDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> DepositResponse:
    if not idempotency_key:
        raise Refused("idempotency_key_required")
    try:
        uuid.UUID(idempotency_key)
    except ValueError:
        raise Refused("idempotency_key_invalid") from None
    if body.payout_method_id is not None:
        stored = methods.destination_for_deposit(session, user_id, _method_id(body.payout_method_id))
        view, created = deposits.create_deposit_to(
            session,
            body.voucher_token,
            stored.destination,
            idempotency_key,
            user_id,
            destination_added_at=stored.added_at,
            risk_acknowledged=body.risk_acknowledged,
        )
    elif body.destination is not None:
        view, created = deposits.create_deposit(
            session,
            body.voucher_token,
            body.destination,
            idempotency_key,
            user_id,
            risk_acknowledged=body.risk_acknowledged,
        )
    else:
        raise Refused("invalid_destination")
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return DepositResponse.of(view)


@router.get("/deposits/{deposit_id}", dependencies=[Depends(limit_deposit_status_by_token)])
def get_deposit(
    deposit_id: str, session: SessionDep, deposits: DepositsDep, user_id: UserIdDep
) -> DepositResponse:
    try:
        parsed = uuid.UUID(deposit_id)
    except ValueError:
        raise Refused("deposit_not_found") from None
    return DepositResponse.of(deposits.get_deposit(session, parsed, user_id))


async def _refused(_request: Request, exc: Exception) -> JSONResponse:
    reason = exc.reason if isinstance(exc, Refused) else "unknown"
    headers = {"Retry-After": str(exc.retry_after_seconds)} if isinstance(exc, RateLimited) else None
    return JSONResponse({"reason": reason}, status_code=REASON_STATUS.get(reason, 400), headers=headers)


def mount(app: FastAPI) -> None:
    app.include_router(router)
    app.add_exception_handler(Refused, _refused)
