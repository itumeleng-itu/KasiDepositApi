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
from datetime import datetime
from zoneinfo import ZoneInfo

PIN_LENGTH = 16
SERIAL_RANDOM_DIGITS = 12
ISSUER_TZ = ZoneInfo("Africa/Johannesburg")


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
