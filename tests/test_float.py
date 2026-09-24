"""The float check: nothing is charged for a payout the float cannot cover.

The concurrency test uses real threads on real, separate connections.
"""

import threading
import time
import uuid
from dataclasses import dataclass, field

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.db import make_session_factory
from app.ledger import (
    Entry,
    InsufficientFloat,
    available_float,
    fund_float,
    lock_and_check_float,
    post,
)
from app.models import Deposit, DepositStatus, LedgerAccount, Voucher, VoucherStatus
from app.switch import VoucherSwitch
from tests.conftest import assert_ledger_balanced

FEE = 500
JOIN_TIMEOUT_SECONDS = 60


@pytest.fixture
def switch() -> VoucherSwitch:
    return VoucherSwitch(min_voucher_cents=1000, max_voucher_cents=500000)


def _setup(engine: Engine, switch: VoucherSwitch, float_cents: int, *amounts: int) -> list[str]:
    with make_session_factory(engine)() as session:
        if float_cents:
            fund_float(session, float_cents)
        pins = [switch.vend(session, amount).pin for amount in amounts]
        session.commit()
    return pins


def _charge_with_float_check(session: Session, switch: VoucherSwitch, pin: str, reference: str) -> None:
    """The order the deposit flow will use: float lock and check, charge, record."""
    amount = switch.lookup(session, pin).amount_cents
    lock_and_check_float(session, amount - FEE)
    charged = switch.charge(session, pin)
    deposit = Deposit(
        reference=reference,
        voucher_pin=pin,
        amount_cents=charged.amount_cents,
        fee_cents=FEE,
        payout_cents=charged.amount_cents - FEE,
        destination={"kind": "shap_id", "shap_id": "+27821234560"},
        status=DepositStatus.CHARGED,
        idempotency_key=str(uuid.uuid4()),
    )
    session.add(deposit)
    session.flush()
    post(
        session,
        deposit.id,
        [
            Entry(LedgerAccount.VOUCHER_RECEIVABLE, charged.amount_cents),
            Entry(LedgerAccount.USER_PAYABLE, -(charged.amount_cents - FEE)),
            Entry(LedgerAccount.FEE_INCOME, -FEE),
        ],
    )


def _status(engine: Engine, pin: str) -> VoucherStatus:
    with make_session_factory(engine)() as session:
        voucher = session.get(Voucher, pin)
        assert voucher is not None
        return voucher.status


def test_available_float_is_settlement_less_what_we_owe(clean_db: Engine, switch: VoucherSwitch) -> None:
    (pin,) = _setup(clean_db, switch, 100_000, 50000)
    with make_session_factory(clean_db)() as session:
        assert available_float(session) == 100_000
        _charge_with_float_check(session, switch, pin, "KD-FLTAAA")
        session.commit()
        # R495 is owed but not yet paid out: it is spoken for.
        assert available_float(session) == 100_000 - 49500


def test_a_payout_the_float_cannot_cover_is_refused_before_charging(
    clean_db: Engine, switch: VoucherSwitch
) -> None:
    (pin,) = _setup(clean_db, switch, 49_499, 50000)  # one cent short of R495
    with make_session_factory(clean_db)() as session:
        with pytest.raises(InsufficientFloat) as excinfo:
            _charge_with_float_check(session, switch, pin, "KD-FLTAAA")
        session.rollback()
        assert excinfo.value.reason == "insufficient_float"
        assert_ledger_balanced(session)
    assert _status(clean_db, pin) is VoucherStatus.ACTIVE  # nothing was charged


def test_a_payout_exactly_equal_to_the_float_is_allowed(clean_db: Engine, switch: VoucherSwitch) -> None:
    (pin,) = _setup(clean_db, switch, 49_500, 50000)
    with make_session_factory(clean_db)() as session:
        _charge_with_float_check(session, switch, pin, "KD-FLTAAA")
        session.commit()
        assert available_float(session) == 0
    assert _status(clean_db, pin) is VoucherStatus.REDEEMED


def test_an_unfunded_float_refuses_everything(clean_db: Engine, switch: VoucherSwitch) -> None:
    (pin,) = _setup(clean_db, switch, 0, 1000)
    with make_session_factory(clean_db)() as session:
        with pytest.raises(InsufficientFloat):
            _charge_with_float_check(session, switch, pin, "KD-FLTAAA")


@dataclass
class Outcomes:
    charged: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    errors: list[Exception] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _attempt(
    factory: sessionmaker[Session],
    switch: VoucherSwitch,
    pin: str,
    reference: str,
    start: threading.Barrier,
    outcomes: Outcomes,
) -> None:
    try:
        with factory() as session:
            start.wait(timeout=JOIN_TIMEOUT_SECONDS)
            try:
                _charge_with_float_check(session, switch, pin, reference)
            except InsufficientFloat:
                session.rollback()
                with outcomes.lock:
                    outcomes.refused.append(pin)
                return
            session.commit()
            with outcomes.lock:
                outcomes.charged.append(pin)
    except Exception as exc:  # surfaced to the test thread below
        with outcomes.lock:
            outcomes.errors.append(exc)


@pytest.mark.parametrize("round_", range(3))
def test_two_deposits_cannot_both_spend_a_float_that_covers_one(
    clean_db: Engine, switch: VoucherSwitch, round_: int
) -> None:
    # R600 of float; two R500 vouchers each need R495. Only one may be charged.
    pins = _setup(clean_db, switch, 60_000, 50000, 50000)
    factory = make_session_factory(clean_db)
    outcomes = Outcomes()
    start = threading.Barrier(2)
    threads = [
        threading.Thread(target=_attempt, args=(factory, switch, pin, ref, start, outcomes))
        for pin, ref in zip(pins, ("KD-FLTAAA", "KD-FLTBBB"), strict=True)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOIN_TIMEOUT_SECONDS)
        assert not t.is_alive()

    assert outcomes.errors == []
    assert len(outcomes.charged) == 1 and len(outcomes.refused) == 1
    assert _status(clean_db, outcomes.charged[0]) is VoucherStatus.REDEEMED
    assert _status(clean_db, outcomes.refused[0]) is VoucherStatus.ACTIVE
    with factory() as session:
        assert available_float(session) == 60_000 - 49500
        assert_ledger_balanced(session)


def test_the_second_float_check_waits_for_the_first_then_is_refused(
    clean_db: Engine, switch: VoucherSwitch
) -> None:
    """Deterministic: A checks the float and charges, holding its transaction
    open; B's float check must block on the float lock (not read the float
    before A's charge is counted), then be refused once A commits."""
    pin_a, pin_b = _setup(clean_db, switch, 60_000, 50000, 50000)
    factory = make_session_factory(clean_db)
    outcomes = Outcomes()
    b_pid: list[int] = []

    def attempt_b() -> None:
        try:
            with factory() as session:
                b_pid.append(session.execute(text("SELECT pg_backend_pid()")).scalar_one())
                try:
                    _charge_with_float_check(session, switch, pin_b, "KD-FLTBBB")
                except InsufficientFloat:
                    session.rollback()
                    outcomes.refused.append(pin_b)
                    return
                session.commit()
                outcomes.charged.append(pin_b)
        except Exception as exc:  # surfaced below
            outcomes.errors.append(exc)

    with factory() as session_a:
        _charge_with_float_check(session_a, switch, pin_a, "KD-FLTAAA")  # holds the float lock
        thread_b = threading.Thread(target=attempt_b)
        thread_b.start()

        waiting_query = ""
        deadline = time.monotonic() + JOIN_TIMEOUT_SECONDS
        with clean_db.connect() as observer:
            while time.monotonic() < deadline:
                if b_pid:
                    row = observer.execute(
                        text("SELECT wait_event_type, query FROM pg_stat_activity WHERE pid = :pid"),
                        {"pid": b_pid[0]},
                    ).one_or_none()
                    observer.rollback()
                    if row is not None and row.wait_event_type == "Lock":
                        waiting_query = row.query
                        break
        assert "pg_advisory_xact_lock" in waiting_query, "B never blocked on the float lock"
        session_a.commit()

    thread_b.join(JOIN_TIMEOUT_SECONDS)
    assert not thread_b.is_alive()
    assert outcomes.errors == []
    assert (outcomes.charged, outcomes.refused) == ([], [pin_b])
    assert _status(clean_db, pin_b) is VoucherStatus.ACTIVE
