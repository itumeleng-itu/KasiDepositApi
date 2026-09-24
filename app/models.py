"""SQLAlchemy models. The Alembic migrations are the source of truth for the
schema; these must match them (tests/test_schema.py checks for drift).

Only `app/switch.py` may touch `Voucher` — everything else goes through the
switch (see its module docstring).

Not visible here, because they live in the migration rather than in table
metadata (see alembic/versions/0004): the `updated_at` triggers on `deposits`
and `payouts`, the append-only trigger on `ledger_entries`, and the deferred
check that every ledger `entry_group` sums to zero.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    MetaData,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.money import Cents

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _pg_enum(enum_cls: type[enum.StrEnum], name: str) -> Enum:
    """A Postgres enum stored by value. Types are created in migrations, not here."""
    return Enum(
        enum_cls,
        name=name,
        values_callable=lambda members: [m.value for m in members],
        create_type=False,
    )


class VoucherStatus(enum.StrEnum):
    ACTIVE = "active"
    REDEEMED = "redeemed"


class Voucher(Base):
    """A single-use, full-value bearer voucher.

    `amount_cents` is the face value and never changes: redemption is all or
    nothing, so `status` alone says whether the money is still there. There is
    deliberately no holder/user reference — the backend never learns who holds
    a voucher.
    """

    __tablename__ = "vouchers"

    pin: Mapped[str] = mapped_column(Text, primary_key=True)
    amount_cents: Mapped[Cents] = mapped_column(BigInteger, nullable=False)
    status: Mapped[VoucherStatus] = mapped_column(
        _pg_enum(VoucherStatus, "voucher_status"), nullable=False
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    serial: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="amount_cents_positive"),
        CheckConstraint("pin ~ '^[0-9]{16}$'", name="pin_format"),
        CheckConstraint("serial ~ '^[0-9]{20}$'", name="serial_format"),
        CheckConstraint(
            "(status = 'active' AND redeemed_at IS NULL)"
            " OR (status = 'redeemed' AND redeemed_at IS NOT NULL)",
            name="redeemed_at_matches_status",
        ),
        Index(
            "ix_vouchers_status_active",
            "status",
            postgresql_where=text("status = 'active'"),
        ),
        # The demo till lists the most recent vouchers.
        Index("ix_vouchers_issued_at", "issued_at"),
    )
    # Fetch server defaults (issued_at) with RETURNING on insert, rather than
    # a second SELECT when they are first read.
    __mapper_args__ = {"eager_defaults": True}


class VoucherToken(Base):
    """An opaque, short-lived, single-use handle for a looked-up voucher.

    The app sends the PIN once, to look the voucher up, and afterwards refers
    to it only by this token. `consumed_at` is set when a deposit is created
    from it; a consumed or expired token cannot create another deposit.
    """

    __tablename__ = "voucher_tokens"

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    voucher_pin: Mapped[str] = mapped_column(Text, ForeignKey("vouchers.pin"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DepositStatus(enum.StrEnum):
    PENDING = "pending"
    CHARGED = "charged"
    PAYING = "paying"
    SETTLED = "settled"
    FAILED = "failed"


class Deposit(Base):
    """One attempt to turn a voucher into a payout. See app/deposits.py.

    `updated_at` is maintained by a database trigger, not application code.
    """

    __tablename__ = "deposits"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    reference: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    voucher_pin: Mapped[str] = mapped_column(Text, ForeignKey("vouchers.pin"), nullable=False)
    amount_cents: Mapped[Cents] = mapped_column(BigInteger, nullable=False)
    fee_cents: Mapped[Cents] = mapped_column(BigInteger, nullable=False)
    payout_cents: Mapped[Cents] = mapped_column(BigInteger, nullable=False)
    # POPIA: for kind "account" this holds a bank account number, which is
    # personal information. Nothing constructs that kind today (the app only
    # sends ShapIDs), but a retention and encryption decision is needed before
    # the account path goes live. It must not go live without one.
    destination: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[DepositStatus] = mapped_column(
        _pg_enum(DepositStatus, "deposit_status"), nullable=False
    )
    failure_reason: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="amount_cents_positive"),
        CheckConstraint("fee_cents >= 0", name="fee_cents_non_negative"),
        CheckConstraint("payout_cents > 0", name="payout_cents_positive"),
        CheckConstraint(
            "payout_cents = amount_cents - fee_cents", name="payout_is_amount_less_fee"
        ),
        CheckConstraint(
            "(status = 'failed') = (failure_reason IS NOT NULL)",
            name="failure_reason_iff_failed",
        ),
        CheckConstraint("reference ~ '^KD-[A-HJ-NP-Z2-9]{6}$'", name="reference_format"),
        CheckConstraint("destination ? 'kind'", name="destination_has_kind"),
    )
    __mapper_args__ = {"eager_defaults": True}


class LedgerAccount(enum.StrEnum):
    SETTLEMENT = "settlement"
    VOUCHER_RECEIVABLE = "voucher_receivable"
    USER_PAYABLE = "user_payable"
    FEE_INCOME = "fee_income"
    CAPITAL = "capital"


class LedgerEntry(Base):
    """One line of a double-entry transaction. See app/ledger.py for the rules.

    Append-only (a trigger rejects UPDATE and DELETE), and every `entry_group`
    must sum to zero (a deferred trigger checks at commit).
    """

    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    deposit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("deposits.id"), index=True
    )
    account: Mapped[LedgerAccount] = mapped_column(
        _pg_enum(LedgerAccount, "ledger_account"), nullable=False
    )
    amount_cents: Mapped[Cents] = mapped_column(BigInteger, nullable=False)
    entry_group: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("amount_cents <> 0", name="amount_cents_non_zero"),
        # Only funding the float (settlement against capital) has no deposit.
        CheckConstraint(
            "deposit_id IS NOT NULL OR account IN ('settlement', 'capital')",
            name="only_funding_without_deposit",
        ),
    )


class PayoutStatus(enum.StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    FAILED = "failed"


class Payout(Base):
    """Our record of the payout instructed for a deposit: one per deposit.

    `updated_at` is maintained by a database trigger, not application code.
    """

    __tablename__ = "payouts"

    deposit_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("deposits.id"), primary_key=True
    )
    provider_ref: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    amount_cents: Mapped[Cents] = mapped_column(BigInteger, nullable=False)
    status: Mapped[PayoutStatus] = mapped_column(
        _pg_enum(PayoutStatus, "payout_status"), nullable=False
    )
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (CheckConstraint("amount_cents > 0", name="amount_cents_positive"),)
    __mapper_args__ = {"eager_defaults": True}
