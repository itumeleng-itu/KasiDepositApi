"""ShapID resolution in the demo: listed numbers, reserved test numbers, and
everyone else's own number accepted. Matches the app's fake and TESTING.md."""

import pytest

from app.shapid import (
    AMBIGUOUS,
    MASKED_NAME,
    NOT_REGISTERED,
    SOMEONE_ELSES,
    SUSPENDED,
    ShapIdAmbiguous,
    ShapIdError,
    ShapIdInvalidFormat,
    ShapIdNotFound,
    ShapIdSuspended,
    resolve,
)

ANY_NUMBERS = ["+27821234560", "+27821234569", "+27721234565", "+27611234561"]


@pytest.mark.parametrize("shap_id", ANY_NUMBERS)
def test_any_number_is_registered_to_whoever_is_adding_it(shap_id: str) -> None:
    resolved = resolve(shap_id, owner_names="Thabo Sipho Mokoena")
    assert (resolved.shap_id, resolved.shap_name, resolved.bank_id) == (shap_id, "T. Mokoena", "capitec")


@pytest.mark.parametrize("shap_id", ANY_NUMBERS)
def test_with_nobody_asking_it_shows_the_placeholder_name(shap_id: str) -> None:
    assert resolve(shap_id).shap_name == MASKED_NAME


@pytest.mark.parametrize(
    ("number", "error"),
    [(NOT_REGISTERED, ShapIdNotFound), (SUSPENDED, ShapIdSuspended), (AMBIGUOUS, ShapIdAmbiguous)],
)
def test_the_reserved_failure_numbers(number: str, error: type[ShapIdError]) -> None:
    with pytest.raises(error):
        resolve(number, owner_names="Thabo Mokoena")


def test_the_ambiguous_number_resolves_once_a_bank_is_chosen() -> None:
    resolved = resolve(f"{AMBIGUOUS}@fnb", owner_names="Thabo Mokoena")
    assert (resolved.shap_name, resolved.bank_id) == ("T. Mokoena", "fnb")


def test_the_someone_else_number_is_never_in_the_adders_name() -> None:
    assert resolve(SOMEONE_ELSES, owner_names="Thabo Mokoena").shap_name == MASKED_NAME


def test_a_suffix_does_not_rescue_a_number_that_is_not_registered() -> None:
    with pytest.raises(ShapIdNotFound):
        resolve(f"{NOT_REGISTERED}@fnb")


@pytest.mark.parametrize(("suffix", "bank"), [("fnb", "fnb"), ("FNB", "fnb"), ("Standard_Bank", "standard_bank")])
def test_a_bank_suffix_places_the_number_at_that_bank(suffix: str, bank: str) -> None:
    assert resolve(f"+27821234560@{suffix}").bank_id == bank


def test_a_listed_number_wins_over_everything() -> None:
    listed = {NOT_REGISTERED: ("L. Mahlangu", "absa")}
    resolved = resolve(NOT_REGISTERED, directory=listed.get, owner_names="Thabo Mokoena")
    assert (resolved.shap_name, resolved.bank_id) == ("L. Mahlangu", "absa")


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
