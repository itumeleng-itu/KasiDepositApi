"""The double-entry ledger: where every cent KasiDeposit handles is accounted for.

The money story
---------------
A person hands R500 cash to a spaza shop and receives a voucher PIN. That cash
is now the shop's, and reaches the voucher issuer through the issuer's own
settlement cycle, not through us.

When she redeems the PIN with us, we owe her R495 (R500 less our R5 fee)
immediately, but the issuer has not paid us yet. So the R495 goes out of a
prefunded settlement account that we control at our sponsor bank (our
"float"), and the issuer reimburses us later.

So we do hold funds, briefly: we pay out of our own float and are reimbursed
afterwards. Three things follow, and the code reflects each:

1. We carry the gap between paying her and being paid by the issuer. That gap
   is `voucher_receivable`.
2. The float is finite. A payout that would overdraw it must fail cleanly as
   `insufficient_float` before anything is charged or instructed.
3. Every cent is traceable. Balances are never stored; they are the sum of
   append-only entries.

Accounts
--------
- `settlement`: our prefunded float at the sponsor bank. An asset.
- `voucher_receivable`: what the issuer owes us for vouchers we have charged.
- `user_payable`: what we owe users between charging a voucher and the
  payout settling.
- `fee_income`: our fee per deposit.
- `capital`: the owners' money that funds the float. Funding posts
  `settlement +X, capital -X`; it is the only movement with no deposit.

Sign convention
---------------
Debits are positive, credits are negative. So asset accounts (`settlement`,
`voucher_receivable`) carry positive balances, and what we owe or have earned
(`user_payable`, `fee_income`, `capital`) carries negative balances. A
successful R500 deposit:

    On charge:           voucher_receivable  +50000   the issuer owes us R500
                         user_payable        -49500   we owe her R495
                         fee_income            -500   we earned R5
    On payout settling:  user_payable        +49500   we no longer owe her
                         settlement          -49500   the money left our float

Rules
-----
- Every transaction is two or more entries, sharing one `entry_group`, that sum
  to exactly zero. `post` refuses anything else, and the database refuses it
  again at commit (a deferred trigger), so bypassing `post` does not help.
- Entries are append-only; the database rejects UPDATE and DELETE. A reversal
  is a new, compensating transaction.
- No zero amounts, and only funding (`settlement` against `capital`) may be
  posted without a deposit.
- Everything is written through `post`.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import BigInteger, cast, func, select, text
from sqlalchemy.orm import Session

from app.models import LedgerAccount, LedgerEntry
from app.money import Cents

FUNDING_ACCOUNTS = frozenset({LedgerAccount.SETTLEMENT, LedgerAccount.CAPITAL})


@dataclass(frozen=True)
class Entry:
    account: LedgerAccount
    amount_cents: Cents  # signed: debit positive, credit negative


class UnbalancedEntries(ValueError):
    """The entries do not form a valid double-entry transaction."""


class InsufficientFloat(Exception):
    """The float cannot cover this payout. Raised before anything is charged."""

    reason = "insufficient_float"

    def __init__(self, payout_cents: Cents, available_cents: Cents) -> None:
        super().__init__(f"payout of {payout_cents} cents exceeds available float of {available_cents}")
        self.payout_cents = payout_cents
        self.available_cents = available_cents


def post(session: Session, deposit_id: uuid.UUID | None, entries: Sequence[Entry]) -> uuid.UUID:
    """Write one balanced transaction and return its `entry_group`.

    Validates everything before writing anything, so a refused transaction
    leaves no rows behind. Flushes but does not commit: the caller owns the
    transaction, so a deposit's status change and its entries commit together.
    """
    if len(entries) < 2:
        raise UnbalancedEntries("a transaction needs at least two entries")
    for entry in entries:
        amount = entry.amount_cents
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise TypeError(f"amount_cents must be an int, got {type(amount).__name__}")
        if amount == 0:
            raise UnbalancedEntries(f"zero-amount entry for {entry.account}")
    total = sum(entry.amount_cents for entry in entries)
    if total != 0:
        raise UnbalancedEntries(f"entries sum to {total}, not zero")
    if deposit_id is None and any(e.account not in FUNDING_ACCOUNTS for e in entries):
        raise UnbalancedEntries("only funding (settlement against capital) may omit the deposit")

    group = uuid.uuid4()
    session.add_all(
        LedgerEntry(
            deposit_id=deposit_id,
            account=entry.account,
            amount_cents=entry.amount_cents,
            entry_group=group,
        )
        for entry in entries
    )
    session.flush()
    return group


def balance_of(session: Session, account: LedgerAccount) -> Cents:
    """The account's balance: the sum of all its entries (debits positive)."""
    # sum(bigint) is numeric in Postgres; cast back so no Decimal ever appears.
    total = session.scalar(
        select(
            cast(func.coalesce(func.sum(LedgerEntry.amount_cents), 0), BigInteger)
        ).where(LedgerEntry.account == account)
    )
    return total or 0


def fund_float(session: Session, amount_cents: Cents) -> uuid.UUID:
    """Top up the settlement float from capital. Does not commit."""
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents <= 0:
        raise ValueError("a float top-up must be a positive number of cents")
    return post(
        session,
        None,
        [
            Entry(LedgerAccount.SETTLEMENT, amount_cents),
            Entry(LedgerAccount.CAPITAL, -amount_cents),
        ],
    )


def available_float(session: Session) -> Cents:
    """What the float can still pay out: the settlement balance, less what we
    already owe users for charged vouchers whose payouts have not settled.

    `user_payable` carries a negative balance while we owe money (credits are
    negative), so adding it subtracts what is owed.
    """
    return balance_of(session, LedgerAccount.SETTLEMENT) + balance_of(
        session, LedgerAccount.USER_PAYABLE
    )


FLOAT_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtext('kasideposit-float:' || current_schema()))")


def lock_float(session: Session) -> None:
    """Take the float lock for the rest of this transaction (see below)."""
    session.execute(FLOAT_LOCK_SQL)


def check_float(session: Session, payout_cents: Cents) -> None:
    """Refuse a payout the float cannot cover. Call with the float lock held."""
    available = available_float(session)
    if payout_cents > available:
        raise InsufficientFloat(payout_cents, available)


def lock_and_check_float(session: Session, payout_cents: Cents) -> None:
    """Serialise float checks, then refuse a payout the float cannot cover.

    Call inside the charging transaction, BEFORE charging the voucher. The
    advisory lock is held until that transaction ends, so a concurrent deposit
    waits here until this one's charge entries (its `user_payable` credit) are
    committed and counted. Without the lock, two deposits could each see the
    whole float and together overdraw it.

    Lock order: every request takes this float lock first and the voucher's
    row lock (`VoucherSwitch.charge`) second, so the two can never deadlock.
    The float lock serialises all deposits, which is fine at demo volume.
    """
    lock_float(session)
    check_float(session, payout_cents)
