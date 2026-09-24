"""create vouchers table and voucher_status enum

Revision ID: 0001
Revises:
Create Date: 2026-09-24

The enum type is created and dropped explicitly: Alembic does not manage
Postgres enum types on its own, and a downgrade that left `voucher_status`
behind would make the next upgrade fail with "type already exists".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

voucher_status = postgresql.ENUM(
    "active", "redeemed", name="voucher_status", create_type=False
)


def upgrade() -> None:
    voucher_status.create(op.get_bind(), checkfirst=False)

    op.create_table(
        "vouchers",
        sa.Column("pin", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("status", voucher_status, nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("serial", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("pin", name="pk_vouchers"),
        sa.UniqueConstraint("serial", name="uq_vouchers_serial"),
        sa.CheckConstraint("amount_cents > 0", name="ck_vouchers_amount_cents_positive"),
        sa.CheckConstraint("pin ~ '^[0-9]{16}$'", name="ck_vouchers_pin_format"),
        sa.CheckConstraint("serial ~ '^[0-9]{20}$'", name="ck_vouchers_serial_format"),
        sa.CheckConstraint(
            "(status = 'active' AND redeemed_at IS NULL)"
            " OR (status = 'redeemed' AND redeemed_at IS NOT NULL)",
            name="ck_vouchers_redeemed_at_matches_status",
        ),
    )
    op.create_index(
        "ix_vouchers_status_active",
        "vouchers",
        ["status"],
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("ix_vouchers_status_active", table_name="vouchers")
    op.drop_table("vouchers")
    voucher_status.drop(op.get_bind(), checkfirst=False)
