"""Request and response bodies for /v1, exactly as the mobile app's
src/api/wire.ts sends and parses them: snake_case throughout.

Request fields that carry a PIN or a destination are typed loosely (a string,
a dict) and validated in app/deposits.py, so a malformed request is refused
with an app reason rather than a validation error that echoes the value back.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.banks import BANK_API_CODES
from app.deposits import DepositRecordView, DepositView, VoucherLookup
from app.payout_methods import PayoutMethodView
from app.money import Cents


class LookupRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pin: str


class LookupResponse(BaseModel):
    voucher_token: str
    value_cents: Cents
    fee_cents: Cents
    payout_cents: Cents

    @classmethod
    def of(cls, lookup: VoucherLookup) -> "LookupResponse":
        return cls(
            voucher_token=lookup.voucher_token,
            value_cents=lookup.value_cents,
            fee_cents=lookup.fee_cents,
            payout_cents=lookup.payout_cents,
        )


class ShapIdResponse(BaseModel):
    shap_name: str  # masked by the scheme (POPIA): an initial and surname
    bank: str  # the app's upper-case bank code, e.g. "CAPITEC"


class CreateDepositRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    voucher_token: str
    # One of the two: a saved payout method (what the app sends), or a
    # destination resolved on the spot (the older shape, kept for tools).
    payout_method_id: str | None = None
    destination: dict[str, Any] | None = None
    # No amount, deliberately: the backend reads it from the voucher row.


class DepositResponse(BaseModel):
    id: uuid.UUID
    reference: str
    status: str
    payout_cents: Cents
    failure_reason: str | None

    @classmethod
    def of(cls, view: DepositView) -> "DepositResponse":
        return cls(
            id=view.id,
            reference=view.reference,
            status=view.status,
            payout_cents=view.payout_cents,
            failure_reason=view.failure_reason,
        )


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Loosely typed and validated in app/users.py, so a bad value is refused
    # with an app reason instead of a validation error that echoes it back.
    full_names: str
    id_number: str

    def __repr__(self) -> str:  # never show the ID number
        return "RegisterRequest(<redacted>)"


class RegisterResponse(BaseModel):
    user_id: uuid.UUID
    access_token: str
    full_names: str


class DepositRecordResponse(BaseModel):
    id: uuid.UUID
    reference: str
    status: str
    payout_cents: Cents
    value_cents: Cents
    fee_cents: Cents
    failure_reason: str | None
    created_at: datetime
    destination: dict[str, Any]

    @classmethod
    def of(cls, view: DepositRecordView) -> "DepositRecordResponse":
        destination = dict(view.destination)
        bank_id = destination.pop("bank_id", None)
        destination["bank"] = BANK_API_CODES.get(str(bank_id))
        return cls(
            id=view.id,
            reference=view.reference,
            status=view.status,
            payout_cents=view.payout_cents,
            value_cents=view.value_cents,
            fee_cents=view.fee_cents,
            failure_reason=view.failure_reason,
            created_at=view.created_at,
            destination=destination,
        )


class DepositHistoryResponse(BaseModel):
    deposits: list[DepositRecordResponse]


class AddPayoutMethodRequest(BaseModel):
    """{kind: "shap_id", shap_id} or {kind: "account", bank, account_number}.
    Loosely typed and checked in app/payout_methods.py, so a bad value is
    refused with an app reason rather than echoed back."""

    model_config = ConfigDict(extra="ignore")

    kind: str
    shap_id: str | None = None
    bank: str | None = None
    account_number: str | None = None
    make_default: bool = False

    def __repr__(self) -> str:  # never show an account number
        return f"AddPayoutMethodRequest(kind={self.kind!r}, <redacted>)"


class PayoutMethodResponse(BaseModel):
    """Never an account number: the app shows accounts by their last four digits."""

    id: uuid.UUID
    kind: str
    bank: str
    is_default: bool
    shap_id: str | None = None
    shap_name: str | None = None
    account_holder: str | None = None
    account_last4: str | None = None

    @classmethod
    def of(cls, view: PayoutMethodView) -> "PayoutMethodResponse":
        return cls(
            id=view.id,
            kind=view.kind,
            bank=BANK_API_CODES[view.bank_id],
            is_default=view.is_default,
            shap_id=view.shap_id,
            shap_name=view.shap_name,
            account_holder=view.account_holder,
            account_last4=view.account_last4,
        )


class PayoutMethodListResponse(BaseModel):
    payout_methods: list[PayoutMethodResponse]
