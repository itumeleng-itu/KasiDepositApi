"""The voucher switch — the seam between KasiDeposit and a voucher issuer.

In production, vouchers belong to a third party: a retail voucher issuer
whose switch we reach over HTTP under a commercial agreement. The spaza's
terminal vends through that switch, and we only ever look up and charge PINs
on it. For the demo, our own database stands in for that switch.

All voucher logic lives here, behind `VoucherSwitch`. Routes and (later) the
deposit flow call this module — never the `vouchers` table directly. Swapping
in a real issuer then means replacing one class and nothing else.

PIN and serial generation live here because they are the issuer's job, not
ours: with a real issuer we would never generate a PIN at all.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from psycopg.errors import UniqueViolation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Voucher, VoucherStatus
from app.money import Cents

PIN_LENGTH = 16
SERIAL_RANDOM_DIGITS = 12
ISSUER_TZ = ZoneInfo("Africa/Johannesburg")
MAX_VEND_ATTEMPTS = 5

# The constraints that make a freshly generated PIN or serial a collision.
_COLLISION_CONSTRAINTS = frozenset({"pk_vouchers", "uq_vouchers_serial"})


def _random_digits(count: int) -> str:
    # randbelow(10) per digit is uniform; mapping hex or bytes onto digits is not.
    return "".join(str(secrets.randbelow(10)) for _ in range(count))


def generate_pin() -> str:
    """A 16-digit voucher PIN. Always a string: leading zeros are legal."""
    return _random_digits(PIN_LENGTH)


def generate_serial(now: datetime | None = None) -> str:
    """Issuer-style reference: YYYYMMDD (South African date) + 12 random digits."""
    if now is None:
        now = datetime.now(ISSUER_TZ)
    elif now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(ISSUER_TZ).strftime("%Y%m%d") + _random_digits(
        SERIAL_RANDOM_DIGITS
    )


# --- What the switch returns -------------------------------------------------
# Plain values, never ORM rows: a real issuer answers over HTTP, so callers
# must not come to depend on our table or hold objects attached to a session.


@dataclass(frozen=True)
class VendedVoucher:
    pin: str
    serial: str
    amount_cents: Cents
    issued_at: datetime


@dataclass(frozen=True)
class VoucherSummary:
    """One row of the demo till's list. The PIN is masked, never whole."""

    serial: str
    pin_masked: str
    amount_cents: Cents
    status: VoucherStatus
    issued_at: datetime
    redeemed_at: datetime | None


@dataclass(frozen=True)
class VoucherInfo:
    """Result of `VoucherSwitch.lookup`. Fields are defined in phase 2."""


@dataclass(frozen=True)
class ChargeResult:
    """Result of `VoucherSwitch.charge`. Fields are defined in phase 2."""


class AmountOutOfRange(ValueError):
    def __init__(self, amount_cents: Cents, min_cents: Cents, max_cents: Cents) -> None:
        super().__init__(
            f"amount_cents must be between {min_cents} and {max_cents}, got {amount_cents}"
        )
        self.amount_cents = amount_cents
        self.min_cents = min_cents
        self.max_cents = max_cents


class VendFailed(RuntimeError):
    """No unique PIN/serial after MAX_VEND_ATTEMPTS tries."""


def mask_pin(pin: str) -> str:
    return "*" * (len(pin) - 4) + pin[-4:]


class VoucherSwitch:
    """The demo issuer switch, backed by our own `vouchers` table.

    Methods take the caller's session and never commit: the caller owns the
    transaction, so phase 2's deposit flow can charge a voucher and write its
    ledger entries atomically.
    """

    def __init__(self, min_voucher_cents: Cents, max_voucher_cents: Cents) -> None:
        self.min_voucher_cents = min_voucher_cents
        self.max_voucher_cents = max_voucher_cents

    def vend(self, session: Session, amount_cents: Cents) -> VendedVoucher:
        """Issue a new active voucher for `amount_cents`.

        The unique constraints are the authority on collisions — there is no
        SELECT-then-INSERT, which would race. Each attempt runs in a savepoint
        so a collision rolls back only that attempt, not the caller's work.
        """
        if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
            raise TypeError(f"amount_cents must be an int, got {type(amount_cents).__name__}")
        if not self.min_voucher_cents <= amount_cents <= self.max_voucher_cents:
            raise AmountOutOfRange(amount_cents, self.min_voucher_cents, self.max_voucher_cents)

        for _ in range(MAX_VEND_ATTEMPTS):
            voucher = Voucher(
                pin=generate_pin(),
                serial=generate_serial(),
                amount_cents=amount_cents,
                status=VoucherStatus.ACTIVE,
            )
            try:
                with session.begin_nested():
                    session.add(voucher)
            except IntegrityError as exc:
                if _is_collision(exc):
                    continue
                raise
            return VendedVoucher(
                pin=voucher.pin,
                serial=voucher.serial,
                amount_cents=voucher.amount_cents,
                issued_at=voucher.issued_at,
            )
        raise VendFailed(f"no unique PIN after {MAX_VEND_ATTEMPTS} attempts")

    def recent(self, session: Session, limit: int = 50) -> list[VoucherSummary]:
        """Most recently issued vouchers, newest first, PINs masked.

        Demo only — it feeds the till screen. A real issuer offers nothing
        like it, and it deliberately has no status filter: it shows state,
        it is not a way to find a PIN to spend.
        """
        rows = session.scalars(
            select(Voucher).order_by(Voucher.issued_at.desc(), Voucher.serial.desc()).limit(limit)
        )
        return [
            VoucherSummary(
                serial=v.serial,
                pin_masked=mask_pin(v.pin),
                amount_cents=v.amount_cents,
                status=v.status,
                issued_at=v.issued_at,
                redeemed_at=v.redeemed_at,
            )
            for v in rows
        ]

    def lookup(self, session: Session, pin: str) -> VoucherInfo:
        """Phase 2: report a voucher's amount and whether it can be charged."""
        raise NotImplementedError("VoucherSwitch.lookup arrives in phase 2")

    def charge(self, session: Session, pin: str) -> ChargeResult:
        """Phase 2: redeem a voucher for its full amount, under a row lock."""
        raise NotImplementedError("VoucherSwitch.charge arrives in phase 2")


def _is_collision(exc: IntegrityError) -> bool:
    return (
        isinstance(exc.orig, UniqueViolation)
        and exc.orig.diag.constraint_name in _COLLISION_CONSTRAINTS
    )
