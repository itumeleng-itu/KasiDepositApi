"""voucher tokens, deposits, the double-entry ledger and payouts

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-24

Enum types and trigger functions are created and dropped explicitly: Alembic
manages neither, and a downgrade that left them behind would break the next
upgrade.

Database-enforced rules, beyond the constraints on each table:

- `deposits.updated_at` and `payouts.updated_at` are maintained by a trigger.
- `ledger_entries` is append-only: a trigger rejects UPDATE and DELETE (a
  reversal is new rows). TRUNCATE is not blocked; tests and `seed.py --reset`
  rely on it.
- Every ledger `entry_group` sums to zero, checked by a deferred constraint
  trigger at commit, so an unbalanced transaction cannot be committed even if
  it bypasses `app.ledger.post`.

Every new table gets row-level security and loses the hosted-API roles'
privileges, as in 0003.

Check-constraint names are wrapped in `op.f()`, marking them as final. Without
it the metadata naming convention ("ck_%(table_name)s_%(constraint_name)s") is
applied on top of the already-prefixed name, giving `ck_deposits_ck_deposits_...`
and, past Postgres's 63-character limit, a truncated name with a hash suffix.
(0001's check constraints on `vouchers` have that doubled prefix.)
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

deposit_status = postgresql.ENUM(
    "pending", "charged", "paying", "settled", "failed", name="deposit_status", create_type=False
)
ledger_account = postgresql.ENUM(
    "settlement",
    "voucher_receivable",
    "user_payable",
    "fee_income",
    "capital",
    name="ledger_account",
    create_type=False,
)
payout_status = postgresql.ENUM(
    "pending", "submitted", "completed", "failed", name="payout_status", create_type=False
)
ENUMS = (deposit_status, ledger_account, payout_status)

NEW_TABLES = ("voucher_tokens", "deposits", "ledger_entries", "payouts")
API_ROLES = ("anon", "authenticated")


def _timestamp(name: str) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in ENUMS:
        enum_type.create(bind, checkfirst=False)

    op.create_table(
        "voucher_tokens",
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("voucher_pin", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("token", name="pk_voucher_tokens"),
        sa.ForeignKeyConstraint(
            ["voucher_pin"], ["vouchers.pin"], name="fk_voucher_tokens_voucher_pin_vouchers"
        ),
    )

    op.create_table(
        "deposits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False),
        sa.Column("voucher_pin", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("fee_cents", sa.BigInteger(), nullable=False),
        sa.Column("payout_cents", sa.BigInteger(), nullable=False),
        # POPIA: see the comment on Deposit.destination in app/models.py.
        sa.Column("destination", postgresql.JSONB(), nullable=False),
        sa.Column("status", deposit_status, nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_deposits"),
        sa.ForeignKeyConstraint(
            ["voucher_pin"], ["vouchers.pin"], name="fk_deposits_voucher_pin_vouchers"
        ),
        sa.UniqueConstraint("reference", name="uq_deposits_reference"),
        sa.UniqueConstraint("idempotency_key", name="uq_deposits_idempotency_key"),
        sa.CheckConstraint("amount_cents > 0", name=op.f("ck_deposits_amount_cents_positive")),
        sa.CheckConstraint("fee_cents >= 0", name=op.f("ck_deposits_fee_cents_non_negative")),
        sa.CheckConstraint("payout_cents > 0", name=op.f("ck_deposits_payout_cents_positive")),
        sa.CheckConstraint(
            "payout_cents = amount_cents - fee_cents",
            name=op.f("ck_deposits_payout_is_amount_less_fee"),
        ),
        sa.CheckConstraint(
            "(status = 'failed') = (failure_reason IS NOT NULL)",
            name=op.f("ck_deposits_failure_reason_iff_failed"),
        ),
        sa.CheckConstraint(
            "reference ~ '^KD-[A-HJ-NP-Z2-9]{6}$'", name=op.f("ck_deposits_reference_format")
        ),
        sa.CheckConstraint("destination ? 'kind'", name=op.f("ck_deposits_destination_has_kind")),
    )

    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("deposit_id", sa.Uuid(), nullable=True),
        sa.Column("account", ledger_account, nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("entry_group", sa.Uuid(), nullable=False),
        _timestamp("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_ledger_entries"),
        sa.ForeignKeyConstraint(
            ["deposit_id"], ["deposits.id"], name="fk_ledger_entries_deposit_id_deposits"
        ),
        sa.CheckConstraint("amount_cents <> 0", name=op.f("ck_ledger_entries_amount_cents_non_zero")),
        sa.CheckConstraint(
            "deposit_id IS NOT NULL OR account IN ('settlement', 'capital')",
            name=op.f("ck_ledger_entries_only_funding_without_deposit"),
        ),
    )
    op.create_index("ix_ledger_entries_deposit_id", "ledger_entries", ["deposit_id"])
    op.create_index("ix_ledger_entries_entry_group", "ledger_entries", ["entry_group"])

    op.create_table(
        "payouts",
        sa.Column("deposit_id", sa.Uuid(), nullable=False),
        sa.Column("provider_ref", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("status", payout_status, nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("deposit_id", name="pk_payouts"),
        sa.ForeignKeyConstraint(
            ["deposit_id"], ["deposits.id"], name="fk_payouts_deposit_id_deposits"
        ),
        sa.UniqueConstraint("provider_ref", name="uq_payouts_provider_ref"),
        sa.CheckConstraint("amount_cents > 0", name=op.f("ck_payouts_amount_cents_positive")),
    )

    # updated_at, maintained by the database rather than application code.
    op.execute(
        """
        CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END
        $$
        """
    )
    for table in ("deposits", "payouts"):
        op.execute(
            f"CREATE TRIGGER {table}_set_updated_at BEFORE UPDATE ON {table}"
            " FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )

    # The ledger is append-only: corrections are new, compensating rows.
    op.execute(
        """
        CREATE FUNCTION ledger_entries_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'ledger_entries is append-only: % is not allowed', TG_OP
                USING ERRCODE = 'restrict_violation';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER ledger_entries_append_only BEFORE UPDATE OR DELETE ON ledger_entries"
        " FOR EACH ROW EXECUTE FUNCTION ledger_entries_append_only()"
    )

    # Every entry_group sums to zero, checked when the transaction commits.
    op.execute(
        """
        CREATE FUNCTION ledger_entry_group_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            total bigint;
        BEGIN
            SELECT sum(amount_cents) INTO total
              FROM ledger_entries WHERE entry_group = NEW.entry_group;
            IF total <> 0 THEN
                RAISE EXCEPTION 'ledger entry_group % does not sum to zero (sum %)',
                    NEW.entry_group, total USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END
        $$
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER ledger_entry_group_balanced AFTER INSERT ON ledger_entries"
        " DEFERRABLE INITIALLY DEFERRED"
        " FOR EACH ROW EXECUTE FUNCTION ledger_entry_group_balanced()"
    )

    for table in NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    for role in API_ROLES:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    REVOKE ALL ON {", ".join(NEW_TABLES)} FROM {role};
                END IF;
            END
            $$
            """
        )


def downgrade() -> None:
    op.drop_table("payouts")
    op.drop_table("ledger_entries")
    op.drop_table("deposits")
    op.drop_table("voucher_tokens")
    op.execute("DROP FUNCTION ledger_entry_group_balanced()")
    op.execute("DROP FUNCTION ledger_entries_append_only()")
    op.execute("DROP FUNCTION set_updated_at()")
    bind = op.get_bind()
    for enum_type in reversed(ENUMS):
        enum_type.drop(bind, checkfirst=False)
