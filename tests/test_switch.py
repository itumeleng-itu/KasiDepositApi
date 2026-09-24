from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import switch
from app.models import Voucher, VoucherStatus
from app.switch import (
    MAX_VEND_ATTEMPTS,
    AmountOutOfRange,
    VendedVoucher,
    VendFailed,
    VoucherSwitch,
    generate_pin,
    generate_serial,
)


def test_pins_are_16_digit_strings_and_distinct_at_volume() -> None:
    pins = [generate_pin() for _ in range(10_000)]
    assert all(isinstance(p, str) for p in pins)
    assert all(len(p) == 16 for p in pins)
    assert all(p.isdigit() and p.isascii() for p in pins)
    assert len(set(pins)) == len(pins)


def test_pin_digits_are_roughly_uniform() -> None:
    # 160 000 digits: expect 16 000 of each (sd ~120). A hex-to-digit mapping
    # would skew by thousands; 800 is ~6.6 sd, so this never flakes.
    counts = Counter("".join(generate_pin() for _ in range(10_000)))
    assert set(counts) == set("0123456789")
    assert all(abs(n - 16_000) < 800 for n in counts.values())


def test_leading_zeros_survive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(switch.secrets, "randbelow", lambda _n: 0)
    pin = generate_pin()
    assert pin == "0000000000000000"
    assert isinstance(pin, str)


def test_serial_is_sa_date_plus_12_digits() -> None:
    serial = generate_serial(datetime(2026, 9, 21, 10, 0, tzinfo=UTC))
    assert len(serial) == 20
    assert serial.isdigit()
    assert serial.startswith("20260921")


def test_serial_date_is_south_african_not_utc() -> None:
    # 23:30 UTC on the 23rd is 01:30 SAST on the 24th.
    assert generate_serial(datetime(2026, 9, 23, 23, 30, tzinfo=UTC)).startswith("20260924")


def test_serial_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        generate_serial(datetime(2026, 9, 21, 10, 0))


def test_serial_random_part_keeps_leading_zeros(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(switch.secrets, "randbelow", lambda _n: 0)
    assert generate_serial(datetime(2026, 9, 21, tzinfo=UTC)) == "20260921" + "0" * 12


def test_serials_are_distinct_at_volume() -> None:
    # Same-day serials share their date, leaving 10^12 random values: 1 000
    # draws collide with probability ~5e-7. (10 000 would be ~5e-5 per run, a
    # real if rare flake; collisions are the unique constraint's job anyway.)
    serials = [generate_serial() for _ in range(1_000)]
    assert len(set(serials)) == len(serials)


# --- VoucherSwitch ---------------------------------------------------------


@pytest.fixture
def voucher_switch() -> VoucherSwitch:
    return VoucherSwitch(min_voucher_cents=1000, max_voucher_cents=500000)


def _count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Voucher)) or 0


def test_vend_creates_an_active_voucher(db_session: Session, voucher_switch: VoucherSwitch) -> None:
    vended = voucher_switch.vend(db_session, 50000)
    db_session.commit()

    assert isinstance(vended, VendedVoucher)
    assert vended.amount_cents == 50000
    assert vended.issued_at.tzinfo is not None

    row = db_session.get(Voucher, vended.pin)
    assert row is not None
    assert row.status is VoucherStatus.ACTIVE
    assert row.redeemed_at is None
    assert row.amount_cents == 50000
    assert row.serial == vended.serial


def test_vend_does_not_commit(db_session: Session, voucher_switch: VoucherSwitch) -> None:
    voucher_switch.vend(db_session, 50000)
    db_session.rollback()
    assert _count(db_session) == 0


@pytest.mark.parametrize("amount", [999, 500001, 0, -1])
def test_vend_rejects_amounts_outside_the_limits(
    db_session: Session, voucher_switch: VoucherSwitch, amount: int
) -> None:
    with pytest.raises(AmountOutOfRange):
        voucher_switch.vend(db_session, amount)


@pytest.mark.parametrize("amount", [1000, 500000])
def test_vend_accepts_the_limits_themselves(
    db_session: Session, voucher_switch: VoucherSwitch, amount: int
) -> None:
    assert voucher_switch.vend(db_session, amount).amount_cents == amount


@pytest.mark.parametrize("amount", [50000.0, True, "50000"])
def test_vend_rejects_non_int_amounts(
    db_session: Session, voucher_switch: VoucherSwitch, amount: object
) -> None:
    with pytest.raises(TypeError):
        voucher_switch.vend(db_session, cast(int, amount))


def _pins(*pins: str) -> Iterator[str]:
    yield from pins


def test_vend_retries_on_pin_collision(
    db_session: Session, voucher_switch: VoucherSwitch, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = voucher_switch.vend(db_session, 10000).pin
    fresh = "9999999999999999" if taken != "9999999999999999" else "8888888888888888"
    supply = _pins(taken, taken, fresh)
    monkeypatch.setattr(switch, "generate_pin", lambda: next(supply))

    vended = voucher_switch.vend(db_session, 20000)
    db_session.commit()

    assert vended.pin == fresh
    # The collisions rolled back only their own savepoints: the first voucher survived.
    assert _count(db_session) == 2


def test_vend_gives_up_after_five_collisions(
    db_session: Session, voucher_switch: VoucherSwitch, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = voucher_switch.vend(db_session, 10000).pin
    calls = 0

    def always_taken() -> str:
        nonlocal calls
        calls += 1
        return taken

    monkeypatch.setattr(switch, "generate_pin", always_taken)
    with pytest.raises(VendFailed):
        voucher_switch.vend(db_session, 20000)
    assert calls == MAX_VEND_ATTEMPTS

    db_session.commit()
    assert _count(db_session) == 1


def test_vend_does_not_retry_other_integrity_errors(
    db_session: Session, voucher_switch: VoucherSwitch, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def malformed() -> str:
        nonlocal calls
        calls += 1
        return "not-a-pin"

    monkeypatch.setattr(switch, "generate_pin", malformed)
    with pytest.raises(IntegrityError, match="ck_vouchers_pin_format"):
        voucher_switch.vend(db_session, 10000)
    assert calls == 1


def test_recent_masks_pins_and_orders_newest_first(
    db_session: Session, voucher_switch: VoucherSwitch
) -> None:
    db_session.execute(
        text(
            "INSERT INTO vouchers (pin, amount_cents, status, serial, issued_at, redeemed_at) VALUES"
            " ('0000000000001111', 10000, 'active',   '20260924000000000001', now() - interval '2 min', NULL),"
            " ('0000000000002222', 20000, 'redeemed', '20260924000000000002', now() - interval '1 min', now())"
        )
    )
    summaries = voucher_switch.recent(db_session)
    assert [s.serial for s in summaries] == ["20260924000000000002", "20260924000000000001"]
    assert summaries[0].pin_masked == "************2222"
    assert summaries[0].status is VoucherStatus.REDEEMED
    assert summaries[0].redeemed_at is not None
    assert summaries[1].pin_masked == "************1111"


def test_lookup_and_charge_are_not_implemented_yet(
    db_session: Session, voucher_switch: VoucherSwitch
) -> None:
    with pytest.raises(NotImplementedError):
        voucher_switch.lookup(db_session, "1234567890123456")
    with pytest.raises(NotImplementedError):
        voucher_switch.charge(db_session, "1234567890123456")
