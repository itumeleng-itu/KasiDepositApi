"""SQLAlchemy models. The Alembic migrations are the source of truth for the
schema; these must match them (tests/test_schema.py checks for drift).

Only `app/switch.py` may touch `Voucher` — everything else goes through the
switch (see its module docstring).
"""

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    Index,
    MetaData,
    Text,
    func,
    text,
)
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
        Enum(
            VoucherStatus,
            name="voucher_status",
            values_callable=lambda members: [m.value for m in members],
            create_type=False,
        ),
        nullable=False,
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
    )
