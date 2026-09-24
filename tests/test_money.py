from typing import cast

import pytest

from app.money import format_rand, rand_to_cents


@pytest.mark.parametrize(
    ("cents", "expected"),
    [
        (0, "R0.00"),
        (5, "R0.05"),
        (99, "R0.99"),
        (100, "R1.00"),
        (50000, "R500.00"),
        (123456, "R1 234.56"),
        (500000, "R5 000.00"),
        (100000000, "R1 000 000.00"),
        (-5, "-R0.05"),
        (-123456, "-R1 234.56"),
    ],
)
def test_format_rand(cents: int, expected: str) -> None:
    assert format_rand(cents) == expected


@pytest.mark.parametrize("bad", [12.5, 100.0, True, "100"])
def test_format_rand_rejects_non_int(bad: object) -> None:
    with pytest.raises(TypeError):
        format_rand(cast(int, bad))


@pytest.mark.parametrize(
    ("rand", "expected"),
    [
        (0, 0),
        (50, 5000),
        (1000, 100000),
        (12.34, 1234),
        (0.1, 10),
        (500.0, 50000),
        ("500", 50000),
        ("0.05", 5),
        ("1 000.00", 100000),
        ("R1 234.56", 123456),
    ],
)
def test_rand_to_cents(rand: str | int | float, expected: int) -> None:
    result = rand_to_cents(rand)
    assert result == expected
    assert type(result) is int


@pytest.mark.parametrize("bad", ["12.345", 0.001, "abc", "", "nan", float("inf")])
def test_rand_to_cents_rejects_bad_amounts(bad: str | float) -> None:
    with pytest.raises(ValueError):
        rand_to_cents(bad)


def test_rand_to_cents_rejects_bool() -> None:
    with pytest.raises(TypeError):
        rand_to_cents(True)
