"""The SA ID algorithm (matching the app's src/domain/saId.ts) and the
encryption of personal numbers. No database."""

from datetime import date

import pytest
from cryptography.exceptions import InvalidTag

from app.pii import ACCOUNT_NUMBER, SA_ID, PiiCipher
from app.sa_id import InvalidSaId, has_valid_checksum, parse_sa_id
from app.users import normalise_names, valid_full_names

TODAY = date(2026, 9, 26)


@pytest.mark.parametrize(
    ("id_number", "dob"),
    [
        ("8001015009087", date(1980, 1, 1)),
        ("7506150123080", date(1975, 6, 15)),
        ("0002295001081", date(2000, 2, 29)),
        ("0809265001085", date(2008, 9, 26)),  # 18 today
    ],
)
def test_valid_ids_parse_with_their_birth_date(id_number: str, dob: date) -> None:
    assert parse_sa_id(id_number, TODAY).date_of_birth == dob


@pytest.mark.parametrize(
    ("id_number", "reason"),
    [
        ("", "id_number_invalid"),
        ("800101500908", "id_number_invalid"),  # 12 digits
        ("80010150090A7", "id_number_invalid"),
        ("9902315001089", "id_number_invalid"),  # 31 February
        ("8001015009384", "id_number_invalid"),  # citizenship digit 3
        ("8001015009088", "id_number_invalid"),  # wrong check digit
        ("0809275001083", "id_number_under_age"),  # 18 tomorrow
    ],
)
def test_invalid_ids_are_refused_with_the_apps_reason(id_number: str, reason: str) -> None:
    with pytest.raises(InvalidSaId) as excinfo:
        parse_sa_id(id_number, TODAY)
    assert excinfo.value.reason == reason
    assert id_number == "" or id_number not in str(excinfo.value)


def test_luhn() -> None:
    assert has_valid_checksum("8001015009087")
    assert not has_valid_checksum("8001015009086")


@pytest.mark.parametrize("names", ["Thabo Mokoena", "Nomvula Grace Dlamini", "Zoë O'Neill-Botha"])
def test_full_names_accepted(names: str) -> None:
    assert valid_full_names(normalise_names(names))


@pytest.mark.parametrize("names", ["Thabo", "Thabo M0koena", "Thabo Mokoena!", "- Thabo", "Thabo " + "a" * 100])
def test_full_names_refused(names: str) -> None:
    assert not valid_full_names(normalise_names(names))


# --- encryption ---------------------------------------------------------------------------

KEY = "k" * 40


def test_round_trip_and_fresh_nonce_each_time() -> None:
    pii = PiiCipher(KEY)
    a, b = pii.encrypt("8001015009087", SA_ID), pii.encrypt("8001015009087", SA_ID)
    assert a != b  # same plaintext, different ciphertext
    assert b"8001015009087" not in a
    assert pii.decrypt(a, SA_ID) == pii.decrypt(b, SA_ID) == "8001015009087"


def test_a_ciphertext_is_bound_to_its_kind() -> None:
    pii = PiiCipher(KEY)
    sealed = pii.encrypt("1234567890", ACCOUNT_NUMBER)
    with pytest.raises(InvalidTag):
        pii.decrypt(sealed, SA_ID)


def test_another_key_cannot_decrypt() -> None:
    sealed = PiiCipher(KEY).encrypt("8001015009087", SA_ID)
    with pytest.raises(InvalidTag):
        PiiCipher("z" * 40).decrypt(sealed, SA_ID)


def test_the_lookup_hash_is_stable_keyed_and_hides_the_number() -> None:
    a, b = PiiCipher(KEY), PiiCipher("z" * 40)
    h = a.lookup_hash("8001015009087", SA_ID)
    assert h == a.lookup_hash("8001015009087", SA_ID)
    assert h != b.lookup_hash("8001015009087", SA_ID)
    assert len(h) == 64 and "8001015009087" not in h


def test_short_keys_and_key_leaks_are_refused() -> None:
    with pytest.raises(ValueError):
        PiiCipher("short")
    assert KEY not in repr(PiiCipher(KEY))
