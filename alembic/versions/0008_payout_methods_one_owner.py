"""payout_methods: a PayShap number or bank account belongs to one user

Revision ID: 0008_payout_methods_one_owner
Revises: 0007_vouchers_idempotency_key
Create Date: 2026-10-25

A PayShap number (and a bank account) pays into one person's account. Until
now the only uniqueness was per user, so two users whose names give the same
masked name ("I. Tjale" for both Itumeleng and Isaac Tjale) could each save
the same number, and every deposit either made would land with its owner.

The per-user unique indexes are replaced by global ones: a number, or a
bank-and-account hash, may appear on one payout method only. A user can
still not save one twice (that is implied), and removing a method frees it.
app/payout_methods.py refuses a second user with shapid_already_linked /
account_already_linked; these indexes decide a race between two users.

If the database already holds a shared number or account, the upgrade stops
before changing anything and says how many: which user keeps it is a human
decision, never one for a migration to make by deleting rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_payout_methods_one_owner"
down_revision: str | None = "0007_vouchers_idempotency_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHARED = """
    SELECT count(*) FROM (
        SELECT {column} FROM payout_methods
        WHERE {column} IS NOT NULL
        GROUP BY {column}
        HAVING count(DISTINCT user_id) > 1
    ) AS shared
"""


def upgrade() -> None:
    conn = op.get_bind()
    shared_numbers = conn.execute(sa.text(_SHARED.format(column="shap_id"))).scalar_one()
    shared_accounts = conn.execute(sa.text(_SHARED.format(column="account_number_hash"))).scalar_one()
    if shared_numbers or shared_accounts:
        raise RuntimeError(
            f"{shared_numbers} PayShap number(s) and {shared_accounts} bank account(s) are saved "
            "by more than one user. Remove the extra payout methods (DELETE /v1/me/payout-methods/"
            "{id} as the user who should not keep it, or delete the rows), then upgrade again. "
            "Nothing was changed. To list the numbers: SELECT shap_id, count(DISTINCT user_id) "
            "FROM payout_methods WHERE shap_id IS NOT NULL GROUP BY shap_id "
            "HAVING count(DISTINCT user_id) > 1;"
        )

    op.drop_index("uq_payout_methods_user_shap_id", table_name="payout_methods")
    op.drop_index("uq_payout_methods_user_account", table_name="payout_methods")
    op.create_index(
        "uq_payout_methods_shap_id",
        "payout_methods",
        ["shap_id"],
        unique=True,
        postgresql_where=sa.text("shap_id IS NOT NULL"),
    )
    op.create_index(
        "uq_payout_methods_account",
        "payout_methods",
        ["account_number_hash"],
        unique=True,
        postgresql_where=sa.text("account_number_hash IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_payout_methods_account", table_name="payout_methods")
    op.drop_index("uq_payout_methods_shap_id", table_name="payout_methods")
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
