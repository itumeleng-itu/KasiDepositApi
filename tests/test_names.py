"""Matching a PayShap number's masked name to a user's registered names. No database."""

import pytest

from app.names import masked_name_matches
from app.schemas.demo_shapid import mask_name, normalise_number


@pytest.mark.parametrize(
    ("masked", "full_names"),
    [
        ("T. Mokoena", "Thabo Mokoena"),
        ("T. Mokoena", "Thabo Sipho Mokoena"),
        ("S. Mokoena", "Thabo Sipho Mokoena"),  # initial of a middle name
        ("t. MOKOENA", "Thabo Mokoena"),
        ("T Mokoena", "Thabo Mokoena"),
        ("Z. O'Neill-Botha", "Zoë ONeill-Botha"),
        ("É. Dlamini", "Emile Dlamini"),
    ],
)
def test_matches(masked: str, full_names: str) -> None:
    assert masked_name_matches(masked, full_names)


@pytest.mark.parametrize(
    ("masked", "full_names"),
    [
        ("M. Mothiba", "Thabo Mokoena"),  # someone else
        ("M. Mokoena", "Thabo Mokoena"),  # a relative: same surname, other initial
        ("T. Mokoena", "Thabo Dlamini"),
        ("T. M*****a", "Thabo Mokoena"),  # a shape we cannot compare: refuse
        ("Mokoena", "Thabo Mokoena"),
        ("T. Mokoena", "Mokoena"),
    ],
)
def test_does_not_match(masked: str, full_names: str) -> None:
    assert not masked_name_matches(masked, full_names)


def test_the_demo_directory_masks_names_and_normalises_numbers() -> None:
    assert mask_name("Thabo Sipho Mokoena") == "T. Mokoena"
    assert normalise_number("082 555 1234") == normalise_number("+27 82 555 1234") == "+27825551234"
    with pytest.raises(ValueError):
        normalise_number("021 555 1234")  # a landline
    with pytest.raises(ValueError):
        mask_name("Thabo")
