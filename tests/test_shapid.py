"""ShapID resolution: every scenario from the app's fake and TESTING.md."""

import pytest

from app.shapid import (
    MASKED_NAME,
    ShapIdAmbiguous,
    ShapIdError,
    ShapIdInvalidFormat,
    ShapIdNotFound,
    ShapIdSuspended,
    resolve,
)


@pytest.mark.parametrize("shap_id", ["+27821234560", "+27821234561", "+27721234565", "+27611234566"])
def test_ordinary_numbers_resolve_to_m_mothiba_at_capitec(shap_id: str) -> None:
    resolved = resolve(shap_id)
    assert (resolved.shap_id, resolved.shap_name, resolved.bank_id) == (shap_id, "M. Mothiba", "capitec")


@pytest.mark.parametrize(
    ("shap_id", "error"),
    [
        ("+27821234569", ShapIdNotFound),
        ("+27821234568", ShapIdSuspended),
        ("+27821234567", ShapIdAmbiguous),
    ],
)
def test_last_digit_scenarios(shap_id: str, error: type[ShapIdError]) -> None:
    with pytest.raises(error):
        resolve(shap_id)


@pytest.mark.parametrize(("suffix", "bank"), [("fnb", "fnb"), ("FNB", "fnb"), ("Standard_Bank", "standard_bank")])
def test_an_ambiguous_number_with_a_bank_suffix_resolves_at_that_bank(suffix: str, bank: str) -> None:
    resolved = resolve(f"+27821234567@{suffix}")
    assert (resolved.shap_name, resolved.bank_id) == (MASKED_NAME, bank)


def test_a_suffix_does_not_rescue_not_found_or_suspended() -> None:
    with pytest.raises(ShapIdNotFound):
        resolve("+27821234569@fnb")
    with pytest.raises(ShapIdSuspended):
        resolve("+27821234568@fnb")


@pytest.mark.parametrize(
    "shap_id",
    [
        "",
        "0821234560",  # local form: the app always sends E.164
        "27821234560",  # no plus
        "+2782123456",  # too short
        "+278212345600",  # too long
        "+27521234560",  # not a mobile prefix (06, 07, 08)
        "+27 82 123 4560",  # unnormalised
        "+27821234567@",  # empty suffix
        "+27821234567@notabank",
        "+27821234567@fnb@absa",
    ],
)
def test_malformed_shapids_are_invalid_format(shap_id: str) -> None:
    with pytest.raises(ShapIdInvalidFormat):
        resolve(shap_id)


def test_reasons_are_the_exact_strings_the_app_expects() -> None:
    assert {cls.reason for cls in (ShapIdInvalidFormat, ShapIdNotFound, ShapIdSuspended, ShapIdAmbiguous)} == {
        "shapid_invalid_format",
        "shapid_not_found",
        "shapid_suspended",
        "shapid_ambiguous",
    }


def test_the_returned_name_is_masked_not_a_full_name() -> None:
    initial, surname = MASKED_NAME.split(" ")
    assert len(initial) == 2 and initial.endswith(".")
    assert surname
