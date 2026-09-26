"""Registration, sign-in and per-user deposits over /v1, and what reaches the
database: never a plaintext ID or account number."""

import base64
import logging
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
from app.models import Deposit, User, UserSession
from app.payouts import MockPayoutProvider, PayoutRef, ProviderStatus
from app.pii import ACCOUNT_NUMBER, SA_ID, PiiCipher
from app.switch import VoucherSwitch
from tests.conftest import REGISTRATION, TEST_PII_KEY, register

ID_NUMBER = REGISTRATION["id_number"]
ACCOUNT = "1234564417"


class RecordingProvider:
    """The mock provider, remembering what destination each payout was sent to."""

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
        yield client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _deposit(api: TestClient, engine: Engine, token: str, destination: Mapping[str, Any]) -> Any:
    with Session(engine) as session:
        fund_float(session, 10_000_000)
        pin = VoucherSwitch(1000, 500000).vend(session, 50000).pin
        session.commit()
    lookup = api.post("/v1/vouchers/lookup", json={"pin": pin}, headers=_bearer(token))
    assert lookup.status_code == 200, lookup.text
    response = api.post(
        "/v1/deposits",
        json={"voucher_token": lookup.json()["voucher_token"], "destination": destination},
        headers={**_bearer(token), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- registering -------------------------------------------------------------------------


def test_registering_creates_a_user_with_no_plaintext_id(
    api: TestClient, clean_db: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    response = api.post("/v1/users", json={**REGISTRATION, "full_names": "  Thabo   Mokoena "})
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"user_id", "access_token", "full_names"}
    assert body["full_names"] == "Thabo Mokoena"
    assert ID_NUMBER not in response.text

    with Session(clean_db) as session:
        user = session.scalars(select(User)).one()
        pii = PiiCipher(TEST_PII_KEY)
        assert pii.decrypt(user.id_number_encrypted, SA_ID) == ID_NUMBER
        assert user.id_number_hash == pii.lookup_hash(ID_NUMBER, SA_ID)
        # Only the token's hash is stored.
        stored = session.scalars(select(UserSession.token_hash)).one()
        assert stored != body["access_token"] and len(stored) == 64
        # Nothing in either table holds the ID number or the token in plaintext.
        for table in ("users", "user_sessions"):
            dump = session.execute(text(f"SELECT row_to_json(t)::text FROM {table} t")).scalars().all()
            assert not any(ID_NUMBER in row or body["access_token"] in row for row in dump)

    assert ID_NUMBER not in caplog.text


def test_the_same_person_again_relinks_and_revokes_the_old_phone(api: TestClient) -> None:
    old = register(api)
    again = api.post("/v1/users", json=REGISTRATION)
    assert again.status_code == 200
    new = again.json()["access_token"]
    assert api.get("/v1/me/deposits", headers=_bearer(new)).status_code == 200
    stale = api.get("/v1/me/deposits", headers=_bearer(old))
    assert (stale.status_code, stale.json()) == (401, {"reason": "not_registered"})


def test_the_same_id_with_different_details_is_refused(api: TestClient) -> None:
    register(api)
    response = api.post("/v1/users", json={**REGISTRATION, "full_names": "Sipho Ndlovu"})
    assert (response.status_code, response.json()) == (409, {"reason": "id_number_already_registered"})


@pytest.mark.parametrize(
    ("overrides", "status", "reason"),
    [
        ({"id_number": "8001015009088"}, 422, "id_number_invalid"),
        ({"id_number": "0809275001083"}, 422, "id_number_under_age"),
        ({"full_names": "Thabo"}, 422, "invalid_registration"),
        ({"id_number": "8001010000081"}, 422, "id_verification_failed"),
        ({"id_number": "8001010001089"}, 409, "id_number_already_registered"),
    ],
)
def test_registration_refusals(
    api: TestClient, clean_db: Engine, overrides: dict[str, str], status: int, reason: str
) -> None:
    response = api.post("/v1/users", json={**REGISTRATION, **overrides})
    assert (response.status_code, response.json()) == (status, {"reason": reason})
    with Session(clean_db) as session:
        assert session.scalars(select(User)).all() == []


def test_without_a_pii_key_nobody_is_registered(clean_db: Engine, test_settings: Settings) -> None:
    settings = test_settings.model_copy(update={"pii_key": None})
    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/users", json=REGISTRATION)
    assert (response.status_code, response.json()) == (503, {"reason": "registration_unavailable"})


# --- signing in ----------------------------------------------------------------------------


@pytest.mark.parametrize("header", [None, "Bearer", "Bearer not-a-real-token", "Basic abc"])
def test_protected_routes_need_a_valid_token(api: TestClient, header: str | None) -> None:
    headers = {"Authorization": header} if header else {}
    for method, path in [
        ("post", "/v1/vouchers/lookup"),
        ("post", "/v1/deposits"),
        ("get", f"/v1/deposits/{uuid.uuid4()}"),
        ("get", "/v1/me/deposits"),
    ]:
        response = api.request(method.upper(), path, json={}, headers=headers)
        assert (response.status_code, response.json()) == (401, {"reason": "not_registered"}), path


def test_shapid_lookup_and_registering_need_no_token(api: TestClient) -> None:
    assert api.get("/v1/shapid/%2B27821234560").status_code == 200


# --- deposits belong to their user ---------------------------------------------------------


def test_deposits_are_private_and_listed_newest_first(
    api: TestClient, clean_db: Engine, provider: RecordingProvider
) -> None:
    alice = register(api)
    bob = register(api, id_number="7506150123080", full_names="Lerato Mahlangu")

    first = _deposit(api, clean_db, alice, {"kind": "shap_id", "shap_id": "+27821234560"})
    second = _deposit(
        api,
        clean_db,
        alice,
        {"kind": "account", "name": "Thabo Mokoena", "account_number": ACCOUNT, "bank": "FNB"},
    )

    # Bob can neither read nor list Alice's deposits.
    other = api.get(f"/v1/deposits/{first['id']}", headers=_bearer(bob))
    assert (other.status_code, other.json()) == (404, {"reason": "deposit_not_found"})
    assert api.get("/v1/me/deposits", headers=_bearer(bob)).json() == {"deposits": []}

    listed = api.get("/v1/me/deposits", headers=_bearer(alice)).json()["deposits"]
    assert [d["id"] for d in listed] == [second["id"], first["id"]]
    assert listed[1]["destination"] == {
        "kind": "shap_id",
        "shap_id": "+27821234560",
        "shap_name": "M. Mothiba",
        "bank": "CAPITEC",
    }
    assert listed[0]["destination"] == {
        "kind": "account",
        "name": "Thabo Mokoena",
        "account_last4": "4417",
        "bank": "FNB",
    }
    assert {k: listed[1][k] for k in ("value_cents", "fee_cents", "payout_cents")} == {
        "value_cents": 50000,
        "fee_cents": 500,
        "payout_cents": 49500,
    }
    assert api.get("/v1/me/deposits?limit=1", headers=_bearer(alice)).json()["deposits"][0]["id"] == second["id"]

    # The account number is stored encrypted, and only the payout call sees it in plaintext.
    with Session(clean_db) as session:
        row = session.get(Deposit, uuid.UUID(second["id"]))
        assert row is not None
        assert "account_number" not in row.destination
        assert ACCOUNT not in str(row.destination)
        sealed = base64.b64decode(row.destination["account_number_encrypted"])
        assert PiiCipher(TEST_PII_KEY).decrypt(sealed, ACCOUNT_NUMBER) == ACCOUNT
    assert provider.destinations[-1]["account_number"] == ACCOUNT


def test_a_replayed_key_from_another_user_is_not_found(api: TestClient, clean_db: Engine) -> None:
    alice = register(api)
    bob = register(api, id_number="7506150123080", full_names="Lerato Mahlangu")
    with Session(clean_db) as session:
        fund_float(session, 10_000_000)
        pin = VoucherSwitch(1000, 500000).vend(session, 50000).pin
        session.commit()
    token = api.post("/v1/vouchers/lookup", json={"pin": pin}, headers=_bearer(alice)).json()["voucher_token"]
    key = str(uuid.uuid4())
    body = {"voucher_token": token, "destination": {"kind": "shap_id", "shap_id": "+27821234560"}}
    assert api.post("/v1/deposits", json=body, headers={**_bearer(alice), "Idempotency-Key": key}).status_code == 201
    replay = api.post("/v1/deposits", json=body, headers={**_bearer(bob), "Idempotency-Key": key})
    assert (replay.status_code, replay.json()) == (404, {"reason": "deposit_not_found"})
