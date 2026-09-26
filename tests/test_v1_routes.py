"""The /v1 API, against the contract in the app's src/api/wire.ts.

Error bodies are compared whole and string for string: the app maps each
reason to the words the user reads. A fake clock drives the mock payout
provider, so nothing sleeps.
"""

import uuid
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.ledger import available_float, fund_float
from app.main import create_app
from app.models import LedgerAccount, LedgerEntry, Voucher, VoucherStatus
from app.payouts import (
    COMPLETED_AFTER_SECONDS,
    MockPayoutProvider,
    PayoutRef,
    PayoutUnavailable,
    ProviderStatus,
)
from app.switch import VoucherSwitch
from tests.conftest import assert_ledger_balanced, sign_in

SHAP = {"kind": "shap_id", "shap_id": "+27821234560"}
FLOAT = 10_000_000  # R100 000


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SwitchableProvider:
    """The mock provider, with an on/off switch for the network."""

    def __init__(self, clock: FakeClock) -> None:
        self.inner = MockPayoutProvider(clock=clock)
        self.reachable = True

    def create(self, amount_cents: int, destination: Mapping[str, Any], idempotency_key: str) -> PayoutRef:
        if not self.reachable:
            raise PayoutUnavailable("network down")
        return self.inner.create(amount_cents, destination, idempotency_key)

    def status(self, payout_ref: PayoutRef) -> ProviderStatus:
        if not self.reachable:
            raise PayoutUnavailable("network down")
        return self.inner.status(payout_ref)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def provider(clock: FakeClock) -> SwitchableProvider:
    return SwitchableProvider(clock)


@pytest.fixture
def api(clean_db: Engine, test_settings: Settings, provider: SwitchableProvider) -> Iterator[TestClient]:
    with TestClient(create_app(test_settings, payout_provider=provider)) as client:
        yield sign_in(client)


def _fund(engine: Engine, cents: int = FLOAT) -> None:
    with Session(engine) as session:
        fund_float(session, cents)
        session.commit()


def _vend(engine: Engine, amount_cents: int = 50000) -> str:
    with Session(engine) as session:
        pin = VoucherSwitch(1000, 500000).vend(session, amount_cents).pin
        session.commit()
    return pin


def _voucher_status(engine: Engine, pin: str) -> VoucherStatus:
    with Session(engine) as session:
        voucher = session.get(Voucher, pin)
        assert voucher is not None
        return voucher.status


def _lookup(api: TestClient, pin: str) -> str:
    response = api.post("/v1/vouchers/lookup", json={"pin": pin})
    assert response.status_code == 200, response.text
    token: str = response.json()["voucher_token"]
    return token


def _deposit(
    api: TestClient, token: str, key: str | None = None, destination: Mapping[str, Any] = SHAP
) -> Any:
    return api.post(
        "/v1/deposits",
        json={"voucher_token": token, "destination": destination},
        headers={"Idempotency-Key": key or str(uuid.uuid4())},
    )


def _charge_groups(engine: Engine) -> int:
    with Session(engine) as session:
        return session.scalar(
            select(func.count(func.distinct(LedgerEntry.entry_group))).where(
                LedgerEntry.account == LedgerAccount.VOUCHER_RECEIVABLE,
                LedgerEntry.amount_cents > 0,
            )
        ) or 0


def _balanced(engine: Engine) -> None:
    with Session(engine) as session:
        assert_ledger_balanced(session)


# --- POST /v1/vouchers/lookup ------------------------------------------------------


def test_lookup_returns_the_app_shape(api: TestClient, clean_db: Engine) -> None:
    pin = _vend(clean_db, 50000)
    response = api.post("/v1/vouchers/lookup", json={"pin": pin})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"voucher_token", "value_cents", "fee_cents", "payout_cents"}
    assert (body["value_cents"], body["fee_cents"], body["payout_cents"]) == (50000, 500, 49500)
    assert isinstance(body["voucher_token"], str) and len(body["voucher_token"]) >= 32
    assert pin not in response.text
    assert _voucher_status(clean_db, pin) is VoucherStatus.ACTIVE  # looking charges nothing


@pytest.mark.parametrize("pin", ["0000000000000000", "123", "12345678901234567", "abcdefghijklmnop", ""])
def test_lookup_of_an_unknown_or_malformed_pin(api: TestClient, pin: str) -> None:
    response = api.post("/v1/vouchers/lookup", json={"pin": pin})
    assert (response.status_code, response.json()) == (404, {"reason": "voucher_not_found"})


def test_lookup_of_a_redeemed_voucher(api: TestClient, clean_db: Engine) -> None:
    _fund(clean_db)
    pin = _vend(clean_db)
    assert _deposit(api, _lookup(api, pin)).status_code == 201
    response = api.post("/v1/vouchers/lookup", json={"pin": pin})
    assert (response.status_code, response.json()) == (409, {"reason": "voucher_already_redeemed"})


# --- GET /v1/shapid/{shap_id} ---------------------------------------------------------


@pytest.mark.parametrize(
    ("shap_id", "expected_status", "expected_body"),
    [
        ("+27821234560", 200, {"shap_name": "M. Mothiba", "bank": "CAPITEC"}),
        ("+27821234569", 404, {"reason": "shapid_not_found"}),
        ("+27821234568", 409, {"reason": "shapid_suspended"}),
        ("+27821234567", 409, {"reason": "shapid_ambiguous"}),
        ("+27821234567@fnb", 200, {"shap_name": "M. Mothiba", "bank": "FNB"}),
        ("+27821234567@standard_bank", 200, {"shap_name": "M. Mothiba", "bank": "STANDARD_BANK"}),
        ("0821234560", 422, {"reason": "shapid_invalid_format"}),
        ("+27821234567@notabank", 422, {"reason": "shapid_invalid_format"}),
    ],
)
def test_shapid_scenarios(
    api: TestClient, shap_id: str, expected_status: int, expected_body: dict[str, str]
) -> None:
    # The app URL-encodes the ShapID: + becomes %2B and @ becomes %40.
    encoded = shap_id.replace("+", "%2B").replace("@", "%40")
    response = api.get(f"/v1/shapid/{encoded}")
    assert (response.status_code, response.json()) == (expected_status, expected_body)


# --- POST /v1/deposits ----------------------------------------------------------------


def test_create_returns_201_with_the_app_shape(api: TestClient, clean_db: Engine) -> None:
    _fund(clean_db)
    pin = _vend(clean_db, 50000)
    response = _deposit(api, _lookup(api, pin))
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "reference", "status", "payout_cents", "failure_reason"}
    uuid.UUID(body["id"])
    assert body["reference"].startswith("KD-") and len(body["reference"]) == 9
    assert body["status"] in {"pending", "submitted"}
    assert (body["payout_cents"], body["failure_reason"]) == (49500, None)
    assert "X-Demo-Surface" not in response.headers
    assert _voucher_status(clean_db, pin) is VoucherStatus.REDEEMED
    _balanced(clean_db)


def test_the_idempotency_key_header_is_required(api: TestClient) -> None:
    response = api.post("/v1/deposits", json={"voucher_token": "x", "destination": SHAP})
    assert (response.status_code, response.json()) == (400, {"reason": "idempotency_key_required"})


def test_the_idempotency_key_must_be_a_uuid(api: TestClient) -> None:
    response = _deposit(api, "x", key="not-a-uuid")
    assert (response.status_code, response.json()) == (400, {"reason": "idempotency_key_invalid"})


def test_the_same_key_twice_returns_one_deposit(api: TestClient, clean_db: Engine) -> None:
    _fund(clean_db)
    token = _lookup(api, _vend(clean_db))
    key = str(uuid.uuid4())
    first = _deposit(api, token, key)
    second = _deposit(api, token, key)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["reference"] == first.json()["reference"]
    assert _charge_groups(clean_db) == 1
    _balanced(clean_db)


def test_a_different_key_for_a_used_token_is_already_redeemed(api: TestClient, clean_db: Engine) -> None:
    _fund(clean_db)
    token = _lookup(api, _vend(clean_db))
    assert _deposit(api, token).status_code == 201
    response = _deposit(api, token)
    assert (response.status_code, response.json()) == (409, {"reason": "voucher_already_redeemed"})
    assert _charge_groups(clean_db) == 1


def test_the_same_key_with_another_voucher_returns_the_first_deposit(
    api: TestClient, clean_db: Engine
) -> None:
    _fund(clean_db)
    first_pin, second_pin = _vend(clean_db), _vend(clean_db)
    key = str(uuid.uuid4())
    first = _deposit(api, _lookup(api, first_pin), key)
    again = _deposit(api, _lookup(api, second_pin), key)
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"]
    assert _voucher_status(clean_db, second_pin) is VoucherStatus.ACTIVE  # its charge rolled back
    assert _charge_groups(clean_db) == 1
    _balanced(clean_db)


def test_an_amount_in_the_body_is_ignored(api: TestClient, clean_db: Engine) -> None:
    _fund(clean_db)
    token = _lookup(api, _vend(clean_db, 50000))
    response = api.post(
        "/v1/deposits",
        json={"voucher_token": token, "destination": SHAP, "amount_cents": 1, "payout_cents": 999999},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 201
    assert response.json()["payout_cents"] == 49500  # from the voucher row, not the client


def test_insufficient_float_charges_nothing(api: TestClient, clean_db: Engine) -> None:
    _fund(clean_db, 49_499)  # one cent short of R495
    pin = _vend(clean_db, 50000)
    token = _lookup(api, pin)
    response = _deposit(api, token)
    assert (response.status_code, response.json()) == (409, {"reason": "insufficient_float"})
    assert _voucher_status(clean_db, pin) is VoucherStatus.ACTIVE
    assert _charge_groups(clean_db) == 0
    # "Try again later" is true: fund the float, and the same token now works.
    _fund(clean_db, 1)
    assert _deposit(api, token).status_code == 201


def test_a_shapid_failure_at_deposit_charges_nothing(api: TestClient, clean_db: Engine) -> None:
    _fund(clean_db)
    pin = _vend(clean_db)
    token = _lookup(api, pin)
    response = _deposit(api, token, destination={"kind": "shap_id", "shap_id": "+27821234569"})
    assert (response.status_code, response.json()) == (404, {"reason": "shapid_not_found"})
    assert _voucher_status(clean_db, pin) is VoucherStatus.ACTIVE
    assert _deposit(api, token).status_code == 201  # the token was not used up


def test_an_unknown_or_expired_token_is_voucher_not_found(api: TestClient, clean_db: Engine) -> None:
    _fund(clean_db)
    assert _deposit(api, "no-such-token").json() == {"reason": "voucher_not_found"}
    token = _lookup(api, _vend(clean_db))
    with Session(clean_db) as session:
        session.execute(
            text("UPDATE voucher_tokens SET expires_at = now() - interval '1 second' WHERE token = :t"),
            {"t": token},
        )
        session.commit()
    response = _deposit(api, token)
    assert (response.status_code, response.json()) == (404, {"reason": "voucher_not_found"})


def test_voucher_too_small_is_refused_on_create(api: TestClient, clean_db: Engine) -> None:
    _fund(clean_db)
    with Session(clean_db) as session:  # R5: only possible by bypassing the vending limits
        session.execute(
            text(
                "INSERT INTO vouchers (pin, amount_cents, status, serial)"
                " VALUES ('5555555555555555', 500, 'active', '20260924555555555555')"
            )
        )
        session.commit()
    token = _lookup(api, "5555555555555555")
    response = _deposit(api, token)
    assert (response.status_code, response.json()) == (422, {"reason": "voucher_too_small"})
    assert _voucher_status(clean_db, "5555555555555555") is VoucherStatus.ACTIVE


def test_voucher_too_small_is_currently_unreachable(test_settings: Settings) -> None:
    # Vending refuses anything under MIN_VOUCHER_CENTS, and the fee is below it,
    # so no vendable voucher is too small today. If either changes, this fails
    # and the rule on create becomes live.
    app = create_app(test_settings)
    deposits = app.state.deposits
    assert not deposits.is_too_small(test_settings.min_voucher_cents)
    assert test_settings.min_voucher_cents > test_settings.fee_cents


@pytest.mark.parametrize(
    "destination",
    [
        {"kind": "bank"},
        {"shap_id": "+27821234560"},
        {"kind": "account", "name": "T. Mokoena", "account_number": "12ab", "bank": "CAPITEC"},
        {"kind": "account", "name": "T. Mokoena", "account_number": "1234567890", "bank": "NOTABANK"},
    ],
)
def test_malformed_destinations_are_refused(
    api: TestClient, clean_db: Engine, destination: dict[str, str]
) -> None:
    _fund(clean_db)
    response = _deposit(api, _lookup(api, _vend(clean_db)), destination=destination)
    assert (response.status_code, response.json()) == (422, {"reason": "invalid_destination"})


# --- GET /v1/deposits/{id} --------------------------------------------------------------


def test_polling_reaches_completed_and_settles_the_ledger(
    api: TestClient, clean_db: Engine, clock: FakeClock
) -> None:
    _fund(clean_db)
    deposit_id = _deposit(api, _lookup(api, _vend(clean_db, 50000))).json()["id"]
    assert api.get(f"/v1/deposits/{deposit_id}").json()["status"] in {"pending", "submitted"}
    clock.advance(COMPLETED_AFTER_SECONDS)
    body = api.get(f"/v1/deposits/{deposit_id}").json()
    assert (body["status"], body["payout_cents"], body["failure_reason"]) == ("completed", 49500, None)
    with Session(clean_db) as session:
        assert available_float(session) == FLOAT - 49500
        assert_ledger_balanced(session)
    # Polling a settled deposit again changes nothing.
    assert api.get(f"/v1/deposits/{deposit_id}").json()["status"] == "completed"
    assert _charge_groups(clean_db) == 1


@pytest.mark.parametrize(
    ("voucher_cents", "reason"),
    [(40500, "bank_processing_error"), (40600, "limit_exceeded"), (40700, "bank_unavailable")],
)
def test_a_failed_payout_reverses_the_charge_so_try_again_works(
    api: TestClient, clean_db: Engine, clock: FakeClock, voucher_cents: int, reason: str
) -> None:
    _fund(clean_db)
    pin = _vend(clean_db, voucher_cents)  # payout = voucher - R5 hits the trigger
    deposit_id = _deposit(api, _lookup(api, pin)).json()["id"]
    clock.advance(COMPLETED_AFTER_SECONDS)
    body = api.get(f"/v1/deposits/{deposit_id}").json()
    assert (body["status"], body["failure_reason"]) == ("failed", reason)

    assert _voucher_status(clean_db, pin) is VoucherStatus.ACTIVE
    with Session(clean_db) as session:
        assert available_float(session) == FLOAT  # nothing owed, nothing paid
        assert_ledger_balanced(session)
    # "Try again": the same PIN looks up and deposits again.
    assert _deposit(api, _lookup(api, pin)).status_code == 201


def test_an_unknown_deposit_is_not_found(api: TestClient) -> None:
    for deposit_id in (str(uuid.uuid4()), "not-a-uuid"):
        response = api.get(f"/v1/deposits/{deposit_id}")
        assert (response.status_code, response.json()) == (404, {"reason": "deposit_not_found"})


def test_a_charged_deposit_with_no_payout_is_recovered_on_the_next_read(
    api: TestClient, clean_db: Engine, provider: SwitchableProvider, clock: FakeClock
) -> None:
    _fund(clean_db)
    pin = _vend(clean_db)
    provider.reachable = False  # the network drops between charging and instructing
    created = _deposit(api, _lookup(api, pin))
    assert created.status_code == 201
    assert created.json()["status"] == "pending"  # charged, no payout yet
    assert _voucher_status(clean_db, pin) is VoucherStatus.REDEEMED

    provider.reachable = True
    deposit_id = created.json()["id"]
    assert api.get(f"/v1/deposits/{deposit_id}").json()["status"] == "submitted"
    clock.advance(COMPLETED_AFTER_SECONDS)
    assert api.get(f"/v1/deposits/{deposit_id}").json()["status"] == "completed"
    _balanced(clean_db)


def test_an_unreachable_provider_while_paying_is_not_a_failure(
    api: TestClient, clean_db: Engine, provider: SwitchableProvider, clock: FakeClock
) -> None:
    _fund(clean_db)
    deposit_id = _deposit(api, _lookup(api, _vend(clean_db))).json()["id"]
    provider.reachable = False
    clock.advance(60)
    assert api.get(f"/v1/deposits/{deposit_id}").json()["status"] == "submitted"
    provider.reachable = True
    assert api.get(f"/v1/deposits/{deposit_id}").json()["status"] == "completed"


def test_no_v1_response_carries_the_demo_header(api: TestClient, clean_db: Engine) -> None:
    for response in (
        api.post("/v1/vouchers/lookup", json={"pin": "0000000000000000"}),
        api.get("/v1/shapid/%2B27821234560"),
        api.get(f"/v1/deposits/{uuid.uuid4()}"),
    ):
        assert "X-Demo-Surface" not in response.headers
