"""Request and response bodies for /v1, exactly as the mobile app's
src/api/wire.ts sends and parses them: snake_case throughout.

Request fields that carry a PIN or a destination are typed loosely (a string,
a dict) and validated in app/deposits.py, so a malformed request is refused
with an app reason rather than a validation error that echoes the value back.
"""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.deposits import DepositView, VoucherLookup
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
    destination: dict[str, Any]
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
