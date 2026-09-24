"""index vouchers.issued_at for the demo till's recent-vouchers list

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_vouchers_issued_at", "vouchers", ["issued_at"])


def downgrade() -> None:
    op.drop_index("ix_vouchers_issued_at", table_name="vouchers")
