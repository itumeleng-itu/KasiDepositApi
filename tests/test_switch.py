from collections import Counter
from datetime import UTC, datetime

import pytest

from app import switch
from app.switch import generate_pin, generate_serial


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
    serials = [generate_serial() for _ in range(10_000)]
    assert len(set(serials)) == len(serials)
