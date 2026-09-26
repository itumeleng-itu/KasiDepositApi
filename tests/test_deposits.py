"""The deposit state machine, and idempotency under real concurrency."""

import itertools
import threading
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.db import make_session_factory
from app.deposits import (
    ALLOWED_TRANSITIONS,
    APP_STATUS,
    DepositService,
    DepositView,
    IllegalTransition,
    new_reference,
    transition,
)
from app.ledger import fund_float
from app.models import Deposit, DepositStatus, LedgerAccount, LedgerEntry
from app.payouts import MockPayoutProvider
from app.switch import VoucherSwitch
from tests.conftest import assert_ledger_balanced, make_user

S = DepositStatus
JOIN_TIMEOUT_SECONDS = 60


def _deposit(status: DepositStatus) -> Deposit:
    return Deposit(status=status, failure_reason=None)


# --- state machine ------------------------------------------------------------------


def test_the_allowed_transitions_are_exactly_the_documented_ones() -> None:
    assert ALLOWED_TRANSITIONS == {
        (S.PENDING, S.CHARGED),
        (S.CHARGED, S.PAYING),
        (S.PAYING, S.SETTLED),
        (S.PENDING, S.FAILED),
        (S.CHARGED, S.FAILED),
        (S.PAYING, S.FAILED),
    }


@pytest.mark.parametrize(("src", "dst"), sorted(ALLOWED_TRANSITIONS))
def test_every_allowed_transition_is_applied(src: DepositStatus, dst: DepositStatus) -> None:
    deposit = _deposit(src)
    transition(deposit, dst, "bank_unavailable" if dst is S.FAILED else None)
    assert deposit.status is dst


ILLEGAL = sorted(
    (src, dst)
    for src, dst in itertools.product(DepositStatus, repeat=2)
    if (src, dst) not in ALLOWED_TRANSITIONS
)


@pytest.mark.parametrize(("src", "dst"), ILLEGAL)
def test_every_illegal_transition_raises_and_writes_nothing(src: DepositStatus, dst: DepositStatus) -> None:
    deposit = _deposit(src)
    with pytest.raises(IllegalTransition):
        transition(deposit, dst, "bank_unavailable" if dst is S.FAILED else None)
    assert deposit.status is src
    assert deposit.failure_reason is None


def test_failing_needs_a_reason_and_nothing_else_may_have_one() -> None:
    with pytest.raises(ValueError):
        transition(_deposit(S.PAYING), S.FAILED)
    with pytest.raises(ValueError):
        transition(_deposit(S.PAYING), S.SETTLED, "bank_unavailable")


def test_every_state_maps_to_a_status_the_app_knows() -> None:
    assert set(APP_STATUS) == set(DepositStatus)
    assert set(APP_STATUS.values()) == {"pending", "submitted", "completed", "failed"}
    assert (APP_STATUS[S.CHARGED], APP_STATUS[S.PAYING], APP_STATUS[S.SETTLED]) == (
        "pending",
        "submitted",
        "completed",
    )


def test_references_use_only_unambiguous_characters() -> None:
    for _ in range(500):
        ref = new_reference()
        assert ref.startswith("KD-") and len(ref) == 9
        assert not set(ref[3:]) & set("O0I1")


# --- idempotency under real concurrency ---------------------------------------------


@dataclass
class Results:
    views: list[tuple[DepositView, bool]] = field(default_factory=list)
    errors: list[Exception] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


def test_two_simultaneous_requests_with_one_key_make_one_deposit(clean_db: Engine) -> None:
    switch = VoucherSwitch(1000, 500000)
    service = DepositService(
        switch=switch,
        provider=MockPayoutProvider(),
        fee_cents=500,
        min_voucher_cents=1000,
        token_ttl=timedelta(minutes=10),
    )
    factory = make_session_factory(clean_db)
    with factory() as session:
        fund_float(session, 1_000_000)
        pin = switch.vend(session, 50000).pin
        session.commit()
        user_id = make_user(session)
        token = service.lookup_voucher(session, pin).voucher_token

    key = str(uuid.uuid4())
    results = Results()
    start = threading.Barrier(2)

    def attempt() -> None:
        try:
            with factory() as session:
                start.wait(timeout=JOIN_TIMEOUT_SECONDS)
                outcome = service.create_deposit(
                    session, token, {"kind": "shap_id", "shap_id": "+27821234560"}, key, user_id
                )
            with results.lock:
                results.views.append(outcome)
        except Exception as exc:  # surfaced below
            with results.lock:
                results.errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOIN_TIMEOUT_SECONDS)
        assert not t.is_alive()

    assert results.errors == []
    assert sorted(created for _, created in results.views) == [False, True]
    assert len({view.id for view, _ in results.views}) == 1
    with Session(clean_db) as session:
        assert session.scalar(select(func.count()).select_from(Deposit)) == 1
        charges = session.scalar(
            select(func.count()).where(
                LedgerEntry.account == LedgerAccount.VOUCHER_RECEIVABLE, LedgerEntry.amount_cents > 0
            )
        )
        assert charges == 1
        assert_ledger_balanced(session)
