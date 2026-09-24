"""VoucherSwitch.lookup and VoucherSwitch.charge, including real concurrency.

The concurrency tests use real, separate database connections in real threads.
Nothing is mocked: the thing under test is Postgres's row lock.
"""

import inspect
import threading
import time
import uuid
from dataclasses import dataclass, field

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.db import make_session_factory
from app.ledger import Entry, post
from app.models import Deposit, DepositStatus, LedgerAccount, LedgerEntry, Voucher, VoucherStatus
from app.switch import AlreadyRedeemed, VoucherNotFound, VoucherSwitch
from tests.conftest import assert_ledger_balanced

FEE = 500
JOIN_TIMEOUT_SECONDS = 60


@pytest.fixture
def switch() -> VoucherSwitch:
    return VoucherSwitch(min_voucher_cents=1000, max_voucher_cents=500000)


def _vend(engine: Engine, switch: VoucherSwitch, amount_cents: int = 50000) -> str:
    with make_session_factory(engine)() as session:
        pin = switch.vend(session, amount_cents).pin
        session.commit()
    return pin


def _voucher(engine: Engine, pin: str) -> Voucher:
    with make_session_factory(engine)() as session:
        voucher = session.get(Voucher, pin)
        assert voucher is not None
        return voucher


# --- lookup ---------------------------------------------------------------------


def test_lookup_reports_amount_and_status_without_charging(
    clean_db: Engine, switch: VoucherSwitch
) -> None:
    pin = _vend(clean_db, switch, 20000)
    with make_session_factory(clean_db)() as session:
        info = switch.lookup(session, pin)
    assert (info.pin, info.amount_cents, info.status) == (pin, 20000, VoucherStatus.ACTIVE)
    assert _voucher(clean_db, pin).status is VoucherStatus.ACTIVE


def test_lookup_reports_a_redeemed_voucher(clean_db: Engine, switch: VoucherSwitch) -> None:
    pin = _vend(clean_db, switch)
    with make_session_factory(clean_db)() as session:
        switch.charge(session, pin)
        session.commit()
        assert switch.lookup(session, pin).status is VoucherStatus.REDEEMED


def test_lookup_of_an_unknown_pin_raises(db_session: Session, switch: VoucherSwitch) -> None:
    with pytest.raises(VoucherNotFound):
        switch.lookup(db_session, "0000000000000000")


# --- charge ---------------------------------------------------------------------


def test_charge_takes_no_amount() -> None:
    # Single-use, full-value: the amount comes from the voucher row, never the caller.
    assert list(inspect.signature(VoucherSwitch.charge).parameters) == ["self", "session", "pin"]


def test_charge_redeems_the_voucher_for_its_full_amount(
    clean_db: Engine, switch: VoucherSwitch
) -> None:
    pin = _vend(clean_db, switch, 50000)
    with make_session_factory(clean_db)() as session:
        result = switch.charge(session, pin)
        session.commit()
    assert (result.pin, result.amount_cents) == (pin, 50000)
    voucher = _voucher(clean_db, pin)
    assert voucher.status is VoucherStatus.REDEEMED
    assert voucher.redeemed_at == result.redeemed_at


def test_charge_does_not_commit(clean_db: Engine, switch: VoucherSwitch) -> None:
    pin = _vend(clean_db, switch)
    with make_session_factory(clean_db)() as session:
        switch.charge(session, pin)
        session.rollback()
    assert _voucher(clean_db, pin).status is VoucherStatus.ACTIVE


def test_a_second_charge_raises_already_redeemed(clean_db: Engine, switch: VoucherSwitch) -> None:
    pin = _vend(clean_db, switch)
    with make_session_factory(clean_db)() as session:
        switch.charge(session, pin)
        session.commit()
        with pytest.raises(AlreadyRedeemed):
            switch.charge(session, pin)


def test_charging_an_unknown_pin_raises(db_session: Session, switch: VoucherSwitch) -> None:
    with pytest.raises(VoucherNotFound):
        switch.charge(db_session, "0000000000000000")


def test_a_pin_with_leading_zeros_can_be_charged(clean_db: Engine, switch: VoucherSwitch) -> None:
    with make_session_factory(clean_db)() as session:
        session.execute(
            text(
                "INSERT INTO vouchers (pin, amount_cents, status, serial)"
                " VALUES ('0000000000000042', 10000, 'active', '20260924000000000042')"
            )
        )
        session.commit()
        assert switch.charge(session, "0000000000000042").amount_cents == 10000


# --- concurrency: real threads, real connections ----------------------------------


@dataclass
class Outcomes:
    won: list[int] = field(default_factory=list)
    already_redeemed: list[int] = field(default_factory=list)
    errors: list[BaseException] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _record_charge(session: Session, pin: str, amount_cents: int, reference: str) -> None:
    """After a successful charge: the deposit row and its charge entries, as the
    deposit flow will write them, in the charging transaction."""
    deposit = Deposit(
        reference=reference,
        voucher_pin=pin,
        amount_cents=amount_cents,
        fee_cents=FEE,
        payout_cents=amount_cents - FEE,
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
            Entry(LedgerAccount.VOUCHER_RECEIVABLE, amount_cents),
            Entry(LedgerAccount.USER_PAYABLE, -(amount_cents - FEE)),
            Entry(LedgerAccount.FEE_INCOME, -FEE),
        ],
    )


def _redeem(
    factory: sessionmaker[Session],
    switch: VoucherSwitch,
    pin: str,
    attempt: int,
    outcomes: Outcomes,
    before_charge: threading.Barrier | None = None,
    backend_pid: list[int] | None = None,
) -> None:
    """One redemption, in the order the deposit flow must use: charge first
    (taking the row lock), then write the deposit and its ledger entries, then
    commit. The loser's whole transaction rolls back."""
    try:
        with factory() as session:
            if backend_pid is not None:
                backend_pid.append(session.execute(text("SELECT pg_backend_pid()")).scalar_one())
            if before_charge is not None:
                before_charge.wait(timeout=JOIN_TIMEOUT_SECONDS)
            try:
                charged = switch.charge(session, pin)
            except AlreadyRedeemed:
                session.rollback()
                with outcomes.lock:
                    outcomes.already_redeemed.append(attempt)
                return
            _record_charge(session, pin, charged.amount_cents, f"KD-RACE{'AB'[attempt] * 2}")
            session.commit()
            with outcomes.lock:
                outcomes.won.append(attempt)
    except Exception as exc:  # surfaced to the test thread by _assert_exactly_one_charge
        with outcomes.lock:
            outcomes.errors.append(exc)


def _assert_exactly_one_charge(engine: Engine, pin: str, outcomes: Outcomes) -> None:
    assert outcomes.errors == []
    assert len(outcomes.won) == 1
    assert len(outcomes.already_redeemed) == 1
    with make_session_factory(engine)() as session:
        voucher = session.get(Voucher, pin)
        assert voucher is not None and voucher.status is VoucherStatus.REDEEMED
        deposits = session.scalars(select(Deposit).where(Deposit.voucher_pin == pin)).all()
        assert [d.status for d in deposits] == [DepositStatus.CHARGED]
        receivable = session.scalar(
            select(func.sum(LedgerEntry.amount_cents)).where(
                LedgerEntry.account == LedgerAccount.VOUCHER_RECEIVABLE
            )
        )
        assert receivable == 50000  # charged once, not twice
        assert_ledger_balanced(session)


@pytest.mark.parametrize("round_", range(3))
def test_two_threads_racing_one_pin_have_exactly_one_winner(
    clean_db: Engine, switch: VoucherSwitch, round_: int
) -> None:
    pin = _vend(clean_db, switch)
    factory = make_session_factory(clean_db)
    outcomes = Outcomes()
    start_together = threading.Barrier(2)
    threads = [
        threading.Thread(
            target=_redeem, args=(factory, switch, pin, attempt, outcomes, start_together)
        )
        for attempt in (0, 1)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOIN_TIMEOUT_SECONDS)
        assert not t.is_alive()
    _assert_exactly_one_charge(clean_db, pin, outcomes)


def test_the_second_charge_waits_on_the_row_lock_then_loses(
    clean_db: Engine, switch: VoucherSwitch
) -> None:
    """Deterministic: A charges and holds its transaction open; B's charge must
    block on the row lock (not read a stale `active`), and lose once A commits.
    Without FOR UPDATE, B would read `active` before A committed and win too."""
    pin = _vend(clean_db, switch)
    factory = make_session_factory(clean_db)
    outcomes = Outcomes()

    with factory() as session_a:
        charged = switch.charge(session_a, pin)  # A now holds the row lock

        b_pid: list[int] = []
        thread_b = threading.Thread(
            target=_redeem, args=(factory, switch, pin, 1, outcomes, None, b_pid)
        )
        thread_b.start()

        # Wait (bounded) until B is blocked on a lock inside its SELECT ... FOR UPDATE.
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
        assert "FOR UPDATE" in waiting_query, "B never blocked on the voucher's row lock"
        assert thread_b.is_alive()

        _record_charge(session_a, pin, charged.amount_cents, "KD-HELDAA")
        session_a.commit()
        outcomes.won.append(0)

    thread_b.join(JOIN_TIMEOUT_SECONDS)
    assert not thread_b.is_alive()
    _assert_exactly_one_charge(clean_db, pin, outcomes)
