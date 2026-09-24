"""The whole demo, through the HTTP API only:

    vend (the till, /demo) -> look up (the app, /v1) -> deposit -> poll -> settled

and then the money: the ledger balances, the voucher is redeemed, the float
paid her R495, we earned R5, and the issuer owes us the full R500.
"""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.ledger import balance_of, fund_float
from app.main import create_app
from app.models import LedgerAccount, Voucher, VoucherStatus
from app.payouts import COMPLETED_AFTER_SECONDS, MockPayoutProvider
from tests.conftest import assert_ledger_balanced

FLOAT = 1_000_000  # R10 000


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def api(clean_db: Engine, test_settings: Settings, clock: FakeClock) -> Iterator[TestClient]:
    assert test_settings.demo_routes_enabled  # the till is part of the demo
    with TestClient(create_app(test_settings, payout_provider=MockPayoutProvider(clock))) as client:
        yield client


def test_vend_lookup_deposit_poll_settled(api: TestClient, clean_db: Engine, clock: FakeClock) -> None:
    with Session(clean_db) as session:
        fund_float(session, FLOAT)
        session.commit()

    # The spaza's till vends a R500 voucher.
    vended = api.post("/demo/vouchers", json={"amount_cents": 50000})
    assert vended.status_code == 201
    pin = vended.json()["pin"]

    # The app: the PIN goes to the server once, and comes back as a token.
    lookup = api.post("/v1/vouchers/lookup", json={"pin": pin})
    assert lookup.status_code == 200
    assert (lookup.json()["value_cents"], lookup.json()["fee_cents"], lookup.json()["payout_cents"]) == (
        50000,
        500,
        49500,
    )

    # She confirms the ShapID, then sends.
    shap = api.get("/v1/shapid/%2B27821234560")
    assert shap.json() == {"shap_name": "M. Mothiba", "bank": "CAPITEC"}
    created = api.post(
        "/v1/deposits",
        json={
            "voucher_token": lookup.json()["voucher_token"],
            "destination": {"kind": "shap_id", "shap_id": "+27821234560"},
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert created.status_code == 201
    deposit = created.json()
    assert deposit["payout_cents"] == 49500
    assert deposit["reference"].startswith("KD-")

    # The app polls until the payout lands.
    seen = [api.get(f"/v1/deposits/{deposit['id']}").json()["status"]]
    clock.now += COMPLETED_AFTER_SECONDS
    final = api.get(f"/v1/deposits/{deposit['id']}").json()
    seen.append(final["status"])
    assert seen[0] in {"pending", "submitted"}
    assert final == {
        "id": deposit["id"],
        "reference": deposit["reference"],
        "status": "completed",
        "payout_cents": 49500,
        "failure_reason": None,
    }

    # The money.
    with Session(clean_db) as session:
        assert_ledger_balanced(session)
        voucher = session.get(Voucher, pin)
        assert voucher is not None and voucher.status is VoucherStatus.REDEEMED
        assert balance_of(session, LedgerAccount.SETTLEMENT) == FLOAT - 49500  # R495 left the float
        assert balance_of(session, LedgerAccount.USER_PAYABLE) == 0  # we owe her nothing
        assert balance_of(session, LedgerAccount.FEE_INCOME) == -500  # we earned R5
        assert balance_of(session, LedgerAccount.VOUCHER_RECEIVABLE) == 50000  # the issuer owes us R500

    # And the PIN cannot be spent twice.
    again = api.post("/v1/vouchers/lookup", json={"pin": pin})
    assert (again.status_code, again.json()) == (409, {"reason": "voucher_already_redeemed"})
