"""The payout worker (app/worker.py) and the one advance path it shares with
the status read (`DepositService.advance`).

A fake clock drives the mock payout provider, so nothing waits for real
time except the race tests, which slow the provider down on purpose so that
two workers are inside the same deposit at once.
"""

import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.deposits import ADVANCE_TRY_LOCK_SQL, ADVANCE_UNLOCK_SQL, DepositService
from app.ledger import fund_float
from app.main import create_app
from app.models import Deposit, DepositStatus, LedgerAccount, LedgerEntry, Payout
from app.payouts import COMPLETED_AFTER_SECONDS, MockPayoutProvider, PayoutRef, ProviderStatus
from app.switch import VoucherSwitch
from app.worker import PayoutWorker
from tests.conftest import assert_ledger_balanced, sign_in

SHAP = {"kind": "shap_id", "shap_id": "+27821234560"}
FLOAT = 10_000_000


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CountingProvider:
    """The mock, counting what it was asked, with optional delays and one-off crashes."""

    def __init__(self, clock: FakeClock) -> None:
        self.inner = MockPayoutProvider(clock=clock)
        self.refs: list[str] = []
        self.status_calls = 0
        self.status_delay = 0.0
        self.crash_after_create = False
        self.crash_after_status = False

    def create(self, amount_cents: int, destination: Mapping[str, Any], idempotency_key: str) -> PayoutRef:
        ref = self.inner.create(amount_cents, destination, idempotency_key)
        self.refs.append(ref.provider_ref)
        if self.crash_after_create:
            self.crash_after_create = False
            raise Crash("killed after the provider accepted the payout")
        return ref

    def status(self, payout_ref: PayoutRef) -> ProviderStatus:
        self.status_calls += 1
        time.sleep(self.status_delay)
        result = self.inner.status(payout_ref)
        if self.crash_after_status:
            self.crash_after_status = False
            raise Crash("killed after the provider said completed")
        return result


class Crash(Exception):
    """Stands in for the process dying at that exact line."""


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def provider(clock: FakeClock) -> CountingProvider:
    return CountingProvider(clock)


@pytest.fixture
def api(clean_db: Engine, test_settings: Settings, provider: CountingProvider) -> Iterator[TestClient]:
    with TestClient(create_app(test_settings, payout_provider=provider)) as client:
        yield sign_in(client)


def _worker(app: FastAPI) -> PayoutWorker:
    return PayoutWorker(
        app.state.session_factory, app.state.deposits, interval_seconds=1, min_age_seconds=0, batch_size=50
    )


def _deposit(api: TestClient, engine: Engine, amount_cents: int = 50000) -> uuid.UUID:
    """A deposit created through the API and never polled after."""
    with Session(engine) as session:
        fund_float(session, FLOAT)
        pin = VoucherSwitch(1000, 500000).vend(session, amount_cents).pin
        session.commit()
    token = api.post("/v1/vouchers/lookup", json={"pin": pin}).json()["voucher_token"]
    created = api.post(
        "/v1/deposits",
        json={"voucher_token": token, "destination": SHAP},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert created.status_code == 201, created.text
    return uuid.UUID(created.json()["id"])


def _status(engine: Engine, deposit_id: uuid.UUID) -> DepositStatus:
    with Session(engine) as session:
        deposit = session.get(Deposit, deposit_id)
        assert deposit is not None
        return deposit.status


def _settlements(engine: Engine, deposit_id: uuid.UUID) -> int:
    with Session(engine) as session:
        return session.scalar(
            select(func.count(func.distinct(LedgerEntry.entry_group))).where(
                LedgerEntry.deposit_id == deposit_id, LedgerEntry.account == LedgerAccount.SETTLEMENT
            )
        ) or 0


def _balanced(engine: Engine) -> None:
    with Session(engine) as session:
        assert_ledger_balanced(session)


# --- the worker on its own ---------------------------------------------------------------------


def test_an_unpolled_deposit_reaches_settled(api: TestClient, clean_db: Engine, clock: FakeClock) -> None:
    deposit_id = _deposit(api, clean_db)
    assert _status(clean_db, deposit_id) is DepositStatus.PAYING  # instructed by the create request

    clock.advance(COMPLETED_AFTER_SECONDS + 1)
    assert _worker(api.app).run_once() == 1  # type: ignore[arg-type]

    assert _status(clean_db, deposit_id) is DepositStatus.SETTLED
    assert _settlements(clean_db, deposit_id) == 1
    _balanced(clean_db)


def test_an_unpolled_deposit_whose_payout_was_never_instructed_is_instructed_then_settled(
    api: TestClient, clean_db: Engine, clock: FakeClock, provider: CountingProvider
) -> None:
    provider.crash_after_create = True  # the create request dies after the provider accepted
    with pytest.raises(Crash):
        _deposit(api, clean_db)
    with Session(clean_db) as session:
        deposit_id = session.scalars(select(Deposit.id)).one()
    assert _status(clean_db, deposit_id) is DepositStatus.CHARGED

    worker = _worker(api.app)  # type: ignore[arg-type]
    worker.run_once()
    assert _status(clean_db, deposit_id) is DepositStatus.PAYING
    clock.advance(COMPLETED_AFTER_SECONDS + 1)
    worker.run_once()
    assert _status(clean_db, deposit_id) is DepositStatus.SETTLED

    # Asked twice, the provider handed back ONE payout: the same idempotency key.
    assert len(provider.refs) == 2 and len(set(provider.refs)) == 1
    with Session(clean_db) as session:
        payout = session.get(Payout, deposit_id)
        assert payout is not None and payout.provider_ref == provider.refs[0]
    _balanced(clean_db)


def test_a_failed_payout_is_reversed_once_by_the_worker(api: TestClient, clean_db: Engine, clock: FakeClock) -> None:
    deposit_id = _deposit(api, clean_db, amount_cents=40500)  # R394.87 payout: bank_processing_error
    clock.advance(COMPLETED_AFTER_SECONDS + 1)
    _worker(api.app).run_once()  # type: ignore[arg-type]
    assert _status(clean_db, deposit_id) is DepositStatus.FAILED
    _worker(api.app).run_once()  # type: ignore[arg-type]  # nothing left to do: no second reversal
    _balanced(clean_db)


def test_a_crash_between_the_provider_and_the_ledger_settles_exactly_once(
    api: TestClient, clean_db: Engine, clock: FakeClock, provider: CountingProvider
) -> None:
    deposit_id = _deposit(api, clean_db)
    clock.advance(COMPLETED_AFTER_SECONDS + 1)
    provider.crash_after_status = True
    worker = _worker(api.app)  # type: ignore[arg-type]
    worker.run_once()  # the crash is caught and logged; nothing was written
    assert _status(clean_db, deposit_id) is DepositStatus.PAYING
    assert _settlements(clean_db, deposit_id) == 0

    worker.run_once()
    assert _status(clean_db, deposit_id) is DepositStatus.SETTLED
    assert _settlements(clean_db, deposit_id) == 1
    _balanced(clean_db)


def test_a_deposit_another_process_holds_is_left_alone(api: TestClient, clean_db: Engine, clock: FakeClock) -> None:
    deposit_id = _deposit(api, clean_db)
    clock.advance(COMPLETED_AFTER_SECONDS + 1)
    with clean_db.connect().execution_options(isolation_level="AUTOCOMMIT") as other:
        assert other.scalar(ADVANCE_TRY_LOCK_SQL, {"key": str(deposit_id)})
        assert _worker(api.app).run_once() == 0  # type: ignore[arg-type]
        assert _status(clean_db, deposit_id) is DepositStatus.PAYING
        other.execute(ADVANCE_UNLOCK_SQL, {"key": str(deposit_id)})
    assert _worker(api.app).run_once() == 1  # type: ignore[arg-type]
    assert _status(clean_db, deposit_id) is DepositStatus.SETTLED


def test_the_worker_leaves_recent_deposits_to_the_polling_app(api: TestClient, clean_db: Engine, clock: FakeClock) -> None:
    deposit_id = _deposit(api, clean_db)
    clock.advance(COMPLETED_AFTER_SECONDS + 1)
    patient = PayoutWorker(
        api.app.state.session_factory, api.app.state.deposits,  # type: ignore[attr-defined]
        interval_seconds=1, min_age_seconds=3600, batch_size=50,
    )
    assert patient.run_once() == 0
    assert _status(clean_db, deposit_id) is DepositStatus.PAYING


# --- races --------------------------------------------------------------------------------------


def _race(*runs: Any) -> None:
    start = threading.Barrier(len(runs))
    errors: list[BaseException] = []

    def go(run: Any) -> None:
        try:
            start.wait()
            run()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(run,)) for run in runs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors, errors


def test_two_workers_racing_one_deposit_settle_it_once(
    api: TestClient, clean_db: Engine, clock: FakeClock, provider: CountingProvider
) -> None:
    deposit_id = _deposit(api, clean_db)
    clock.advance(COMPLETED_AFTER_SECONDS + 1)
    provider.status_delay = 0.5  # both are inside the deposit at once
    first, second = _worker(api.app), _worker(api.app)  # type: ignore[arg-type]
    calls_before = provider.status_calls

    _race(first.run_once, second.run_once)

    assert provider.status_calls - calls_before == 1  # the advisory lock kept the second one out
    assert _status(clean_db, deposit_id) is DepositStatus.SETTLED
    assert _settlements(clean_db, deposit_id) == 1
    _balanced(clean_db)


def test_without_the_advisory_lock_the_row_lock_recheck_still_settles_once(
    api: TestClient, clean_db: Engine, clock: FakeClock, provider: CountingProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The advisory lock is a courtesy. Without it both workers really are inside the deposit
    (both ask the provider), and the re-check under the row lock lets only one write."""

    @contextmanager
    def no_lock(self: DepositService, session: Session, deposit_id: uuid.UUID) -> Iterator[bool]:
        yield True

    monkeypatch.setattr(DepositService, "_advance_lock", no_lock)
    deposit_id = _deposit(api, clean_db)
    clock.advance(COMPLETED_AFTER_SECONDS + 1)
    provider.status_delay = 0.5
    first, second = _worker(api.app), _worker(api.app)  # type: ignore[arg-type]
    calls_before = provider.status_calls

    _race(first.run_once, second.run_once)

    assert provider.status_calls - calls_before == 2  # the race really happened
    assert _status(clean_db, deposit_id) is DepositStatus.SETTLED
    assert _settlements(clean_db, deposit_id) == 1
    _balanced(clean_db)


def test_a_worker_and_a_polling_app_racing_settle_once(
    api: TestClient, clean_db: Engine, clock: FakeClock, provider: CountingProvider
) -> None:
    deposit_id = _deposit(api, clean_db)
    clock.advance(COMPLETED_AFTER_SECONDS + 1)
    provider.status_delay = 0.5
    worker = _worker(api.app)  # type: ignore[arg-type]

    _race(worker.run_once, lambda: api.get(f"/v1/deposits/{deposit_id}"))

    # Whichever lost may have read it a moment before; one more read shows the outcome.
    assert api.get(f"/v1/deposits/{deposit_id}").json()["status"] == "completed"
    assert _settlements(clean_db, deposit_id) == 1
    _balanced(clean_db)


# --- off by default -----------------------------------------------------------------------------


def test_the_worker_is_off_unless_switched_on(test_settings: Settings) -> None:
    assert create_app(test_settings).state.payout_worker is None
    on = create_app(test_settings.model_copy(update={"payout_worker_enabled": True}))
    assert isinstance(on.state.payout_worker, PayoutWorker)
