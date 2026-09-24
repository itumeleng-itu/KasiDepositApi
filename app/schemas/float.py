"""Request and response bodies for the demo float endpoints."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.money import Cents, format_rand

MAX_TOP_UP_CENTS = 100_000_000  # R1 000 000 per top-up


class FundFloatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # StrictInt: 1000.0 or "1000" is a 422, never silently coerced.
    amount_cents: Annotated[StrictInt, Field(gt=0, le=MAX_TOP_UP_CENTS)]


class FloatResponse(BaseModel):
    settlement_cents: Cents
    settlement_display: str
    available_cents: Cents  # the settlement balance less what we owe but have not paid out
    available_display: str

    @classmethod
    def of(cls, settlement_cents: Cents, available_cents: Cents) -> "FloatResponse":
        return cls(
            settlement_cents=settlement_cents,
            settlement_display=format_rand(settlement_cents),
            available_cents=available_cents,
            available_display=format_rand(available_cents),
        )
