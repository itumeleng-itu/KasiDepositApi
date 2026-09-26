"""Payout methods over /v1/me/payout-methods, the demo PayShap directory, and
deposits sent to a saved method."""

import base64
import uuid
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.ledger import fund_float
from app.main import create_app
from app.models import Deposit, PayoutMethod
from app.payouts import MockPayoutProvider, PayoutRef, ProviderStatus
from app.pii import ACCOUNT_NUMBER, PiiCipher
from app.switch import VoucherSwitch
from tests.conftest import TEST_PII_KEY, sign_in

PRESENTER = "+27825551234"
ACCOUNT = "1234564417"
METHODS = "/v1/me/payout-methods"


class RecordingProvider:
    def __init__(self) -> None:
        self.inner = MockPayoutProvider()
        self.destinations: list[Mapping[str, Any]] = []

    def create(self, amount_cents: int, destination: Mapping[str, Any], idempotency_key: str) -> PayoutRef:
        self.destinations.append(dict(destination))
        return self.inner.create(amount_cents, destination, idempotency_key)

    def status(self, payout_ref: PayoutRef) -> ProviderStatus:
        return self.inner.status(payout_ref)


@pytest.fixture
def provider() -> RecordingProvider:
    return RecordingProvider()


@pytest.fixture
def api(clean_db: Engine, test_settings: Settings, provider: RecordingProvider) -> Iterator[TestClient]:
    with TestClient(create_app(test_settings, payout_provider=provider)) as client:
        # The presenter's number, listed in the demo directory in their own name.
        listed = client.post(
            "/demo/shapids", json={"number": "082 555 1234", "full_name": "Thabo Mokoena", "bank": "FNB"}
        )
        assert listed.status_code == 201, listed.text
        yield sign_in(client)  # registers Thabo Mokoena


def _add(api: TestClient, **body: Any) -> Any:
    return api.post(METHODS, json=body)


# --- the demo directory --------------------------------------------------------------------


def test_the_directory_resolves_a_listed_number_to_its_person(api: TestClient) -> None:
    assert api.get("/v1/shapid/%2B27825551234").json() == {"shap_name": "T. Mokoena", "bank": "FNB"}
    assert api.get("/demo/shapids").json() == [
        {"number": PRESENTER, "shap_name": "T. Mokoena", "bank": "FNB"}
    ]
    assert api.delete("/demo/shapids/0825551234").status_code == 204
    # Unlisted again: back to the scripted scenario for its last digit.
    assert api.get("/v1/shapid/%2B27825551234").json()["shap_name"] == "M. Mothiba"


# --- adding a PayShap number ---------------------------------------------------------------


def test_the_users_own_number_is_added_and_becomes_the_default(api: TestClient) -> None:
    response = _add(api, kind="shap_id", shap_id=PRESENTER)
    assert response.status_code == 201
    assert response.json() == {
        "id": response.json()["id"],
        "kind": "shap_id",
        "bank": "FNB",
        "is_default": True,
        "shap_id": PRESENTER,
        "shap_name": "T. Mokoena",
        "account_holder": None,
        "account_last4": None,
    }
    again = _add(api, kind="shap_id", shap_id=PRESENTER)
    assert (again.status_code, again.json()["id"]) == (200, response.json()["id"])


@pytest.mark.parametrize(
    ("shap_id", "status", "reason"),
    [
        ("+27821234560", 409, "shapid_name_mismatch"),  # resolves to M. Mothiba: not this user
        ("+27821234569", 404, "shapid_not_found"),  # not registered for PayShap
        ("+27821234568", 409, "shapid_suspended"),
        ("+27821234567", 409, "shapid_ambiguous"),
        ("0825551234", 422, "shapid_invalid_format"),
    ],
)
def test_a_number_that_is_not_theirs_is_refused(
    api: TestClient, clean_db: Engine, shap_id: str, status: int, reason: str
) -> None:
    response = _add(api, kind="shap_id", shap_id=shap_id)
    assert (response.status_code, response.json()) == (status, {"reason": reason})
    with Session(clean_db) as session:
        assert session.scalars(select(PayoutMethod)).all() == []


def test_someone_else_cannot_add_the_presenters_number(api: TestClient) -> None:
    sign_in(api, id_number="7506150123080", full_names="Lerato Mahlangu")
    response = _add(api, kind="shap_id", shap_id=PRESENTER)
    assert (response.status_code, response.json()) == (409, {"reason": "shapid_name_mismatch"})


# --- adding a bank account ----------------------------------------------------------------


def test_an_account_is_verified_encrypted_and_held_in_the_users_name(
    api: TestClient, clean_db: Engine
) -> None:
    response = _add(api, kind="account", bank="CAPITEC", account_number=ACCOUNT)
    assert response.status_code == 201
    body = response.json()
    assert {k: body[k] for k in ("kind", "bank", "account_holder", "account_last4", "is_default")} == {
        "kind": "account",
        "bank": "CAPITEC",
        "account_holder": "Thabo Mokoena",
        "account_last4": "4417",
        "is_default": True,
    }
    assert ACCOUNT not in response.text
    with Session(clean_db) as session:
        row = session.scalars(select(PayoutMethod)).one()
        assert row.account_number_encrypted is not None
        assert PiiCipher(TEST_PII_KEY).decrypt(row.account_number_encrypted, ACCOUNT_NUMBER) == ACCOUNT
        dump = session.execute(text("SELECT row_to_json(p)::text FROM payout_methods p")).scalar_one()
        assert ACCOUNT not in dump


@pytest.mark.parametrize(
    ("account_number", "bank", "status", "reason"),
    [
        ("1234564419", "CAPITEC", 404, "account_not_found"),
        ("1234564418", "CAPITEC", 409, "account_holder_mismatch"),
        ("12345", "CAPITEC", 422, "invalid_account"),
        (ACCOUNT, "NOT_A_BANK", 422, "invalid_account"),
    ],
)
def test_an_account_that_does_not_check_out_is_refused(
    api: TestClient, account_number: str, bank: str, status: int, reason: str
) -> None:
    response = _add(api, kind="account", bank=bank, account_number=account_number)
    assert (response.status_code, response.json()) == (status, {"reason": reason})


# --- choosing, removing, limits ------------------------------------------------------------


def test_default_switching_and_removal(api: TestClient) -> None:
    shap = _add(api, kind="shap_id", shap_id=PRESENTER).json()
    account = _add(api, kind="account", bank="FNB", account_number=ACCOUNT).json()
    assert account["is_default"] is False  # the first one added stays the default

    listed = api.post(f"{METHODS}/{account['id']}/default").json()["payout_methods"]
    assert [(m["id"], m["is_default"]) for m in listed] == [(account["id"], True), (shap["id"], False)]

    # Removing the default promotes the one that is left.
    listed = api.delete(f"{METHODS}/{account['id']}").json()["payout_methods"]
    assert [(m["id"], m["is_default"]) for m in listed] == [(shap["id"], True)]

    missing = api.delete(f"{METHODS}/{uuid.uuid4()}")
    assert (missing.status_code, missing.json()) == (404, {"reason": "payout_method_not_found"})


def test_at_most_five_methods(api: TestClient) -> None:
    for last in range(5):
        assert _add(api, kind="account", bank="ABSA", account_number=f"123456441{last}").status_code == 201
    extra = _add(api, kind="account", bank="ABSA", account_number="1234564415")
    assert (extra.status_code, extra.json()) == (409, {"reason": "payout_method_limit"})


def test_another_users_method_is_invisible(api: TestClient) -> None:
    mine = _add(api, kind="shap_id", shap_id=PRESENTER).json()
    sign_in(api, id_number="7506150123080", full_names="Lerato Mahlangu")
    assert api.get(METHODS).json() == {"payout_methods": []}
    response = api.post(f"{METHODS}/{mine['id']}/default")
    assert (response.status_code, response.json()) == (404, {"reason": "payout_method_not_found"})


# --- deposits to a saved method --------------------------------------------------------------


def _deposit_to(api: TestClient, engine: Engine, method_id: str) -> Any:
    with Session(engine) as session:
        fund_float(session, 10_000_000)
        pin = VoucherSwitch(1000, 500000).vend(session, 50000).pin
        session.commit()
    token = api.post("/v1/vouchers/lookup", json={"pin": pin}).json()["voucher_token"]
    return api.post(
        "/v1/deposits",
        json={"voucher_token": token, "payout_method_id": method_id},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )


def test_a_deposit_to_a_saved_account_snapshots_it_and_pays_the_real_number(
    api: TestClient, clean_db: Engine, provider: RecordingProvider
) -> None:
    method = _add(api, kind="account", bank="FNB", account_number=ACCOUNT).json()
    created = _deposit_to(api, clean_db, method["id"])
    assert created.status_code == 201, created.text

    with Session(clean_db) as session:
        row = session.get(Deposit, uuid.UUID(created.json()["id"]))
        assert row is not None
        assert row.destination["account_last4"] == "4417"
        assert ACCOUNT not in str(row.destination)
        sealed = base64.b64decode(row.destination["account_number_encrypted"])
        assert PiiCipher(TEST_PII_KEY).decrypt(sealed, ACCOUNT_NUMBER) == ACCOUNT
    assert provider.destinations[-1]["account_number"] == ACCOUNT

    # History shows where it went, never the number.
    [record] = api.get("/v1/me/deposits").json()["deposits"]
    assert record["destination"] == {
        "kind": "account",
        "name": "Thabo Mokoena",
        "account_last4": "4417",
        "bank": "FNB",
    }


def test_a_deposit_to_a_saved_payshap_number(api: TestClient, clean_db: Engine) -> None:
    method = _add(api, kind="shap_id", shap_id=PRESENTER).json()
    created = _deposit_to(api, clean_db, method["id"])
    assert created.status_code == 201, created.text
    [record] = api.get("/v1/me/deposits").json()["deposits"]
    assert record["destination"] == {
        "kind": "shap_id",
        "shap_id": PRESENTER,
        "shap_name": "T. Mokoena",
        "bank": "FNB",
    }


def test_a_deposit_to_someone_elses_method_is_refused(api: TestClient, clean_db: Engine) -> None:
    theirs = _add(api, kind="shap_id", shap_id=PRESENTER).json()
    sign_in(api, id_number="7506150123080", full_names="Lerato Mahlangu")
    response = _deposit_to(api, clean_db, theirs["id"])
    assert (response.status_code, response.json()) == (404, {"reason": "payout_method_not_found"})
