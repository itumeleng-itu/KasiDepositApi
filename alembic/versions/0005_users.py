"""users, their sessions and payout methods; deposits linked to users; the demo PayShap directory

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-26

- `users`: one row per registered person: names and SA ID number only. The
  ID number is stored as a keyed hash (`id_number_hash`, unique: one account
  per ID) and AES-GCM ciphertext (`id_number_encrypted`); see app/pii.py. The
  database never holds it, or the key, in plaintext.
- `user_sessions`: bearer tokens, stored as SHA-256 only.
- `payout_methods`: where a user can be paid, added after registering and
  checked when added: a PayShap number (resolved in the directory, and the
  name on it matches the user) or a bank account (verified to belong to the
  user's ID number). Account numbers are ciphertext, with a keyed hash to
  refuse adding the same account twice. At most one default per user.
- `deposits.user_id`: nullable only because earlier deposits predate users.
  `deposits.destination` may no longer carry a plaintext `account_number`;
  the upgrade refuses to run if any existing row still has one.
- `demo_shapids`: DEMO SCAFFOLDING. Our stand-in for PayShap's proxy
  directory (app/shapid.py), filled from the till page, so a presenter's own
  number resolves to their own name and bank.

Every new table gets row-level security and loses the hosted-API roles'
privileges, as in 0003 and 0004. `updated_at` on `users` reuses 0004's
set_updated_at() trigger function.
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

user_status = postgresql.ENUM("active", "suspended", name="user_status", create_type=False)
payout_method_kind = postgresql.ENUM(
    "shap_id", "account", name="payout_method_kind", create_type=False
)
ENUMS = (user_status, payout_method_kind)

NEW_TABLES = ("users", "user_sessions", "payout_methods", "demo_shapids")
API_ROLES = ("anon", "authenticated")
SHAP_NUMBER = r"^\+27[678][0-9]{8}$"


def _timestamp(name: str) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in ENUMS:
        enum_type.create(bind, checkfirst=False)

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("full_names", sa.Text(), nullable=False),
        sa.Column("id_number_hash", sa.Text(), nullable=False),
        sa.Column("id_number_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("date_of_birth", sa.Date(), nullable=False),
        sa.Column("status", user_status, server_default="active", nullable=False),
        sa.Column("verification_ref", sa.Text(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("id_number_hash", name="uq_users_id_number_hash"),
        sa.CheckConstraint(
            "char_length(full_names) BETWEEN 3 AND 100", name=op.f("ck_users_full_names_length")
        ),
        sa.CheckConstraint(
            "id_number_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_users_id_number_hash_format")
        ),
        sa.CheckConstraint(
            "octet_length(id_number_encrypted) > 12",
            name=op.f("ck_users_id_number_encrypted_present"),
        ),
    )
    op.execute(
        "CREATE TRIGGER users_set_updated_at BEFORE UPDATE ON users"
        " FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        _timestamp("created_at"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_user_sessions"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_user_sessions_user_id_users"),
        sa.UniqueConstraint("token_hash", name="uq_user_sessions_token_hash"),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_user_sessions_token_hash_format")
        ),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])

    op.create_table(
        "payout_methods",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", payout_method_kind, nullable=False),
        sa.Column("bank_id", sa.Text(), nullable=False),
        sa.Column("shap_id", sa.Text(), nullable=True),
        sa.Column("shap_name", sa.Text(), nullable=True),
        sa.Column("account_holder", sa.Text(), nullable=True),
        sa.Column("account_last4", sa.Text(), nullable=True),
        sa.Column("account_number_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("account_number_hash", sa.Text(), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("verification_ref", sa.Text(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        _timestamp("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_payout_methods"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_payout_methods_user_id_users"),
        sa.CheckConstraint(
            "(kind = 'shap_id' AND shap_id IS NOT NULL AND shap_name IS NOT NULL"
            " AND account_holder IS NULL AND account_last4 IS NULL"
            " AND account_number_encrypted IS NULL AND account_number_hash IS NULL)"
            " OR (kind = 'account' AND shap_id IS NULL AND shap_name IS NULL"
            " AND account_holder IS NOT NULL AND account_last4 ~ '^[0-9]{4}$'"
            " AND octet_length(account_number_encrypted) > 12"
            " AND account_number_hash ~ '^[0-9a-f]{64}$')",
            name=op.f("ck_payout_methods_fields_match_kind"),
        ),
        sa.CheckConstraint(
            r"shap_id IS NULL OR shap_id ~ '^\+27[678][0-9]{8}(@[a-z_]+)?$'",
            name=op.f("ck_payout_methods_shap_id_format"),
        ),
    )
    op.create_index("ix_payout_methods_user_id", "payout_methods", ["user_id"])
    op.create_index(
        "uq_payout_methods_one_default",
        "payout_methods",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.create_index(
        "uq_payout_methods_user_shap_id",
        "payout_methods",
        ["user_id", "shap_id"],
        unique=True,
        postgresql_where=sa.text("shap_id IS NOT NULL"),
    )
    op.create_index(
        "uq_payout_methods_user_account",
        "payout_methods",
        ["user_id", "account_number_hash"],
        unique=True,
        postgresql_where=sa.text("account_number_hash IS NOT NULL"),
    )

    op.create_table(
        "demo_shapids",
        sa.Column("number", sa.Text(), nullable=False),
        sa.Column("shap_name", sa.Text(), nullable=False),
        sa.Column("bank_id", sa.Text(), nullable=False),
        _timestamp("created_at"),
        sa.PrimaryKeyConstraint("number", name="pk_demo_shapids"),
        sa.CheckConstraint(f"number ~ '{SHAP_NUMBER}'", name=op.f("ck_demo_shapids_number_format")),
        sa.CheckConstraint(
            "char_length(shap_name) BETWEEN 3 AND 60", name=op.f("ck_demo_shapids_shap_name_length")
        ),
    )

    op.add_column("deposits", sa.Column("user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_deposits_user_id_users", "deposits", "users", ["user_id"], ["id"])
    op.create_index(
        "ix_deposits_user_id_created_at", "deposits", ["user_id", sa.text("created_at DESC")]
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM deposits WHERE destination ? 'account_number') THEN
                RAISE EXCEPTION 'deposits holds plaintext account numbers: encrypt them first';
            END IF;
        END
        $$
        """
    )
    op.create_check_constraint(
        op.f("ck_deposits_destination_no_plain_account_number"),
        "deposits",
        "NOT (destination ? 'account_number')",
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
    op.drop_constraint(
        op.f("ck_deposits_destination_no_plain_account_number"), "deposits", type_="check"
    )
    op.drop_index("ix_deposits_user_id_created_at", table_name="deposits")
    op.drop_constraint("fk_deposits_user_id_users", "deposits", type_="foreignkey")
    op.drop_column("deposits", "user_id")
    op.drop_table("demo_shapids")
    op.drop_table("payout_methods")
    op.drop_table("user_sessions")
    op.drop_table("users")  # drops its trigger with it; set_updated_at() belongs to 0004
    bind = op.get_bind()
    for enum_type in reversed(ENUMS):
        enum_type.drop(bind, checkfirst=False)
