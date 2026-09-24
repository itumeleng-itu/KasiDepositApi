"""post(), balance_of() and the zero-sum rule, in code and in the database."""

import uuid
from typing import cast

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.ledger import Entry, UnbalancedEntries, balance_of, fund_float, post
from app.models import LedgerAccount, LedgerEntry

S = LedgerAccount.SETTLEMENT
VR = LedgerAccount.VOUCHER_RECEIVABLE
UP = LedgerAccount.USER_PAYABLE
FEE = LedgerAccount.FEE_INCOME
CAP = LedgerAccount.CAPITAL

PIN = "1234567890123456"


def assert_ledger_balanced(session: Session) -> None:
    """The whole table sums to zero, and so does every entry_group in it."""
    total = session.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)))
    assert total == 0
    unbalanced = session.execute(
        select(LedgerEntry.entry_group)
        .group_by(LedgerEntry.entry_group)
        .having(func.sum(LedgerEntry.amount_cents) != 0)
    ).all()
    assert unbalanced == []


def _row_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(LedgerEntry)) or 0


@pytest.fixture
def deposit_id(db_session: Session) -> uuid.UUID:
    """A real deposit row for deposit-linked entries to reference."""
    db_session.execute(
        text(
            "INSERT INTO vouchers (pin, amount_cents, status, redeemed_at, serial)"
            " VALUES (:pin, 50000, 'redeemed', now(), '20260924123456789012')"
        ),
        {"pin": PIN},
    )
    new_id = uuid.uuid4()
    db_session.execute(
        text(
            "INSERT INTO deposits (id, reference, voucher_pin, amount_cents, fee_cents,"
            " payout_cents, destination, status, idempotency_key)"
            " VALUES (:id, 'KD-7F3A9C', :pin, 50000, 500, 49500,"
            " '{\"kind\": \"shap_id\"}', 'charged', 'key-1')"
        ),
        {"id": new_id, "pin": PIN},
    )
    db_session.commit()
    return new_id


def test_the_r500_example_from_the_brief(db_session: Session, deposit_id: uuid.UUID) -> None:
    fund_float(db_session, 1_000_000)
    charge = post(db_session, deposit_id, [Entry(VR, 50000), Entry(UP, -49500), Entry(FEE, -500)])
    settle = post(db_session, deposit_id, [Entry(UP, 49500), Entry(S, -49500)])
    db_session.commit()

    assert charge != settle
    assert balance_of(db_session, VR) == 50000  # the issuer owes us R500
    assert balance_of(db_session, UP) == 0  # we no longer owe her anything
    assert balance_of(db_session, FEE) == -500  # we earned R5
    assert balance_of(db_session, S) == 1_000_000 - 49500  # R495 left the float
    assert balance_of(db_session, CAP) == -1_000_000
    assert_ledger_balanced(db_session)


def test_post_writes_one_group_and_returns_it(db_session: Session, deposit_id: uuid.UUID) -> None:
    group = post(db_session, deposit_id, [Entry(VR, 50000), Entry(UP, -49500), Entry(FEE, -500)])
    db_session.commit()
    rows = db_session.scalars(select(LedgerEntry).where(LedgerEntry.entry_group == group)).all()
    assert len(rows) == 3
    assert {r.deposit_id for r in rows} == {deposit_id}


def test_balance_of_an_account_with_no_entries_is_zero(db_session: Session) -> None:
    assert balance_of(db_session, S) == 0
    assert isinstance(balance_of(db_session, S), int)


@pytest.mark.parametrize(
    "entries",
    [
        [Entry(VR, 50000), Entry(UP, -49500)],  # does not sum to zero
        [Entry(VR, 50000)],  # a single entry
        [],  # nothing
        [Entry(VR, 0), Entry(UP, 0)],  # zero amounts
    ],
)
def test_post_refuses_invalid_transactions_and_writes_nothing(
    db_session: Session, deposit_id: uuid.UUID, entries: list[Entry]
) -> None:
    with pytest.raises(UnbalancedEntries):
        post(db_session, deposit_id, entries)
    assert _row_count(db_session) == 0


@pytest.mark.parametrize("amount", [49500.0, True, "49500"])
def test_post_refuses_non_int_amounts(
    db_session: Session, deposit_id: uuid.UUID, amount: object
) -> None:
    entries = [Entry(UP, cast(int, amount)), Entry(S, -49500)]
    with pytest.raises(TypeError):
        post(db_session, deposit_id, entries)
    assert _row_count(db_session) == 0


def test_only_funding_may_be_posted_without_a_deposit(db_session: Session) -> None:
    with pytest.raises(UnbalancedEntries, match="only funding"):
        post(db_session, None, [Entry(S, 500), Entry(UP, -500)])
    assert _row_count(db_session) == 0


@pytest.mark.parametrize("amount", [0, -100, 10.5])
def test_fund_float_needs_a_positive_whole_number_of_cents(
    db_session: Session, amount: object
) -> None:
    with pytest.raises(ValueError):
        fund_float(db_session, cast(int, amount))


def test_funding_raises_the_float_and_balances(db_session: Session) -> None:
    fund_float(db_session, 500_000)
    fund_float(db_session, 250_000)
    db_session.commit()
    assert balance_of(db_session, S) == 750_000
    assert balance_of(db_session, CAP) == -750_000
    assert_ledger_balanced(db_session)
