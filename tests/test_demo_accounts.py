import io
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import make_session_factory
from app.ledger import fund_float
from app.models import DemoShapId, PayoutMethod, User
from app.sa_id import parse_sa_id
from app.schemas.demo_shapid import normalise_number
from demo_accounts import ACCOUNTS, parse_args, run
from tests.conftest import vend_headers


@pytest.fixture
def dev_settings(test_settings: Settings) -> Settings:
    # As in test_seed.py: development rules, but still pointed at TEST_SCHEMA.
    return test_settings.model_copy(
        update={"environment": "development", "dev_schema": test_settings.test_schema}
    )


def _create(clean_db: Engine, settings: Settings) -> str:
    out = io.StringIO()
    assert run(parse_args([]), settings, make_session_factory(clean_db), out) == 0, out.getvalue()
    return out.getvalue()


def test_the_accounts_are_distinct_and_their_ids_valid() -> None:
    assert len({a.id_number for a in ACCOUNTS}) == len(ACCOUNTS)
    assert len({normalise_number(a.payshap_number) for a in ACCOUNTS}) == len(ACCOUNTS)
    for a in ACCOUNTS:
        parse_sa_id(a.id_number, date.today())  # raises if not


def test_creates_each_account_listed_on_payshap_with_a_default_method(
    clean_db: Engine, dev_settings: Settings
) -> None:
    printed = _create(clean_db, dev_settings)

    with Session(clean_db) as session:
        assert session.scalar(select(func.count()).select_from(User)) == len(ACCOUNTS)
        for a in ACCOUNTS:
            number = normalise_number(a.payshap_number)
            assert session.get(DemoShapId, number) is not None
            user = session.scalar(select(User).where(User.full_names == a.full_names))
            assert user is not None and user.shap_id == number and user.verified_at is not None
            method = session.scalar(select(PayoutMethod).where(PayoutMethod.user_id == user.id))
            assert method is not None and method.is_default and method.shap_id == number
            assert a.id_number in printed
    assert "created" in printed


def test_running_again_changes_nothing(clean_db: Engine, dev_settings: Settings) -> None:
    _create(clean_db, dev_settings)
    printed = _create(clean_db, dev_settings)

    assert "already existed" in printed
    with Session(clean_db) as session:
        assert session.scalar(select(func.count()).select_from(User)) == len(ACCOUNTS)
        assert session.scalar(select(func.count()).select_from(PayoutMethod)) == len(ACCOUNTS)
        assert session.scalar(select(func.count()).select_from(DemoShapId)) == len(ACCOUNTS)


@pytest.mark.parametrize("account", ACCOUNTS, ids=lambda a: a.label)
def test_register_screen_signs_into_the_account_and_a_deposit_pays_its_payshap(
    clean_db: Engine, dev_settings: Settings, client: TestClient, account
) -> None:
    _create(clean_db, dev_settings)
    with Session(clean_db) as session:
        fund_float(session, 1_000_000)
        session.commit()

    # What the presenter or judge types on the register screen.
    signed_in = client.post(
        "/v1/users", json={"full_names": account.full_names, "id_number": account.id_number}
    )
    assert signed_in.status_code == 200  # the existing account, not a new one
    client.headers["Authorization"] = f"Bearer {signed_in.json()['access_token']}"

    methods = client.get("/v1/me/payout-methods").json()["payout_methods"]
    assert len(methods) == 1
    assert methods[0]["is_default"] and methods[0]["shap_id"] == normalise_number(account.payshap_number)

    pin = client.post("/demo/vouchers", json={"amount_cents": 100000}, headers=vend_headers()).json()["pin"]
    token = client.post("/v1/vouchers/lookup", json={"pin": pin}).json()["voucher_token"]
    deposit = client.post(
        "/v1/deposits",
        json={"voucher_token": token, "payout_method_id": methods[0]["id"]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert deposit.status_code == 201, deposit.text
    assert deposit.json()["payout_cents"] == 97500


def test_refuses_outside_development(clean_db: Engine, test_settings: Settings) -> None:
    settings = test_settings.model_copy(update={"dev_schema": test_settings.test_schema})
    out = io.StringIO()
    assert run(parse_args([]), settings, make_session_factory(clean_db), out) == 1
    assert "Refusing" in out.getvalue()
    with Session(clean_db) as session:
        assert session.scalar(select(func.count()).select_from(User)) == 0


def test_refuses_without_a_pii_key(clean_db: Engine, dev_settings: Settings) -> None:
    out = io.StringIO()
    settings = dev_settings.model_copy(update={"pii_key": None})
    assert run(parse_args([]), settings, make_session_factory(clean_db), out) == 1
    assert "PII_KEY" in out.getvalue()
