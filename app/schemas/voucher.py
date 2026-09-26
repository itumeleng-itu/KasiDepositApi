"""Request and response models for the demo vending routes."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, StrictInt

from app.models import VoucherStatus
from app.money import Cents, format_rand
from app.qr import voucher_payload, voucher_qr_svg
from app.switch import VendedVoucher, VoucherSummary


class VendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # StrictInt: 50000.0, "50000" and true are all 422s, never silently coerced.
    # Bounds come from settings at request time (see routes/demo.py).
    amount_cents: StrictInt


class VendResponse(BaseModel):
    pin: str
    serial: str
    amount_cents: Cents
    amount_display: str
    issued_at: datetime
    # For the till slip: the QR the mobile app scans (see app/qr.py).
    qr_payload: str
    qr_svg: str

    @classmethod
    def from_vended(cls, vended: VendedVoucher) -> "VendResponse":
        return cls(
            pin=vended.pin,
            serial=vended.serial,
            amount_cents=vended.amount_cents,
            amount_display=format_rand(vended.amount_cents),
            issued_at=vended.issued_at,
            qr_payload=voucher_payload(vended.pin),
            qr_svg=voucher_qr_svg(vended.pin),
        )


class VoucherListItem(BaseModel):
    serial: str
    pin_masked: str
    amount_cents: Cents
    amount_display: str
    status: VoucherStatus
    issued_at: datetime
    redeemed_at: datetime | None

    @classmethod
    def from_summary(cls, summary: VoucherSummary) -> "VoucherListItem":
        return cls(
            serial=summary.serial,
            pin_masked=summary.pin_masked,
            amount_cents=summary.amount_cents,
            amount_display=format_rand(summary.amount_cents),
            status=summary.status,
            issued_at=summary.issued_at,
            redeemed_at=summary.redeemed_at,
        )
