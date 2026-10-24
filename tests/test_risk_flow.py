"""Deposit risk against the real database: what a warn and a review leave
behind (nothing), the confirm-and-resend path, replays, and failing open.

The bands are forced through /demo/risk, which is how the behaviour is shown
on stage; the rules themselves are covered in tests/test_risk.py.
"""

import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.deposits import DepositService
from app.ledger import fund_float
from app.main import create_app
from app.models import Deposit, LedgerEntry, Voucher, VoucherStatus
from app.risk import DepositFacts, RiskDecision
from app.switch import VoucherSwitch
from tests.conftest import sign_in

SHAP = {"kind": "shap_id", "shap_id": "+27821234560"}
FLOAT = 10_000_000


@pytest.fixture
def api(clean_db: Engine, test_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(test_settings.model_copy(update={"risk_enabled": True}))) as client:
        yield sign_in(client)


def _vend(engine: Engine, amount_cents: int = 50000) -> str:
    with Session(engine) as session:
        fund_float(session, FLOAT)
        pin = VoucherSwitch(1000, 500000).vend(session, amount_cents).pin
        session.commit()
    return pin


def _lookup(api: TestClient, pin: str) -> str:
    response = api.post("/v1/vouchers/lookup", json={"pin": pin})
    assert response.status_code == 200, response.text
    token: str = response.json()["voucher_token"]
    return token


def _deposit(api: TestClient, token: str, key: str, **extra: Any) -> Any:
    return api.post(
        "/v1/deposits",
        json={"voucher_token": token, "destination": SHAP, **extra},
        headers={"Idempotency-Key": key},
    )


def _untouched(engine: Engine, pin: str) -> bool:
    with Session(engine) as session:
        voucher = session.get(Voucher, pin)
        deposits = session.scalar(select(func.count()).select_from(Deposit))
        entries = session.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.deposit_id.is_not(None)))
    return voucher is not None and voucher.status is VoucherStatus.ACTIVE and deposits == 0 and entries == 0


def test_an_ordinary_deposit_is_allowed(api: TestClient, clean_db: Engine) -> None:
    pin = _vend(clean_db)
    assert _deposit(api, _lookup(api, pin), str(uuid.uuid4())).status_code == 201


def test_a_warn_charges_nothing_until_confirmed(api: TestClient, clean_db: Engine) -> None:
    api.post("/demo/risk", json={"band": "warn"})
    pin = _vend(clean_db)
    token, key = _lookup(api, pin), str(uuid.uuid4())

    first = _deposit(api, token, key)
    assert (first.status_code, first.json()) == (409, {"reason": "confirmation_required"})
    assert _untouched(clean_db, pin)

    confirmed = _deposit(api, token, key, risk_acknowledged=True)
    assert confirmed.status_code == 201
    # And a replay of the confirmed request is the same deposit, not a second warn.
    assert _deposit(api, token, key).status_code == 200


def test_a_review_charges_nothing_and_leaves_the_voucher_redeemable(api: TestClient, clean_db: Engine) -> None:
    api.post("/demo/risk", json={"band": "review"})
    pin = _vend(clean_db)
    token = _lookup(api, pin)
    response = _deposit(api, token, str(uuid.uuid4()), risk_acknowledged=True)
    assert (response.status_code, response.json()) == (403, {"reason": "deposit_declined"})
    assert _untouched(clean_db, pin)

    api.delete("/demo/risk")
    assert _deposit(api, token, str(uuid.uuid4())).status_code == 201


def test_a_replay_is_never_refused_by_risk(api: TestClient, clean_db: Engine) -> None:
    token, key = _lookup(api, _vend(clean_db)), str(uuid.uuid4())
    assert _deposit(api, token, key).status_code == 201
    api.post("/demo/risk", json={"band": "review"})
    replay = _deposit(api, token, key)
    assert replay.status_code == 200  # the app's retry after a timeout still finds its deposit


class Exploding:
    def assess(self, facts: DepositFacts) -> RiskDecision:
        raise RuntimeError("a scoring bug")


def test_a_broken_assessor_fails_open_end_to_end(
    api: TestClient, clean_db: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    deposits: DepositService = api.app.state.deposits  # type: ignore[attr-defined]
    deposits.risk = Exploding()
    caplog.set_level(logging.ERROR, logger="app.risk")
    response = _deposit(api, _lookup(api, _vend(clean_db)), str(uuid.uuid4()))
    assert response.status_code == 201
    assert "FAILING OPEN" in caplog.text


def test_real_rules_warn_on_a_burst(api: TestClient, clean_db: Engine) -> None:
    # Vended and redeemed within seconds (lag_short) and the fourth in an hour (velocity_1h).
    for _ in range(3):
        assert _deposit(api, _lookup(api, _vend(clean_db, 20000)), str(uuid.uuid4())).status_code == 201
    response = _deposit(api, _lookup(api, _vend(clean_db, 20000)), str(uuid.uuid4()))
    assert (response.status_code, response.json()) == (409, {"reason": "confirmation_required"})
