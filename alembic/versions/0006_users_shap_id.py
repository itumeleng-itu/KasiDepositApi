"""users.shap_id: a copy of the user's current PayShap number

Revision ID: 0006_users_shap_id
Revises: 0005
Create Date: 2026-09-27

Set when a user adds a PayShap number or chooses one as their default, and
moved to their newest remaining PayShap number (or cleared) when it is
removed; see app/payout_methods.py. `payout_methods` stays the source of
truth for where money goes. Plaintext, like `payout_methods.shap_id`, which
holds the same value, with the same format check.

Existing users are backfilled from their payout methods: the default one if
it is a PayShap number, else their newest PayShap number.

The revision id is not a bare number on purpose: a shared test schema has
been stamped with revisions "0006"/"0007" that are not in this repo, and a
bare "0006" here could be mistaken for one of them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_users_shap_id"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("shap_id", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_users_shap_id_format"),
        "users",
        r"shap_id IS NULL OR shap_id ~ '^\+27[678][0-9]{8}(@[a-z_]+)?$'",
    )
    op.execute(
        """
        UPDATE users SET shap_id = (
            SELECT pm.shap_id FROM payout_methods pm
            WHERE pm.user_id = users.id AND pm.kind = 'shap_id'
            ORDER BY pm.is_default DESC, pm.created_at DESC, pm.id
            LIMIT 1
        )
        """
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_users_shap_id_format"), "users", type_="check")
    op.drop_column("users", "shap_id")
