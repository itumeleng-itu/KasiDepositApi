"""vouchers.idempotency_key: make a repeated sale safe

Revision ID: 0007_vouchers_idempotency_key
Revises: 0006_users_shap_id
Create Date: 2026-10-23

The till takes the cash before it asks for a voucher, so a request that
times out or loses the connection leaves the cashier unable to tell a
voucher that was issued from one that was not. Vending again costs the shop
a second voucher; refusing to retry strands the customer's cash. Neither is
acceptable at a counter with a queue.

With this column the vending client sends a key per sale and a retry
returns the voucher the first call issued (app/switch.py `vend`). Only the
unique constraint decides, never a read-then-insert: two taps racing on one
key must not both insert.

Nullable, because every voucher issued before this column existed has no
key, and Postgres permits many NULLs under a unique constraint. Vouchers
vended without a key keep the old behaviour — a retry vends again — so the
guarantee belongs to clients that send one.

The revision id is not a bare number on purpose: a shared test schema has
been stamped with revisions "0006"/"0007" that are not in this repo, and a
bare "0007" here could be mistaken for one of them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_vouchers_idempotency_key"
down_revision: str | None = "0006_users_shap_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("vouchers", sa.Column("idempotency_key", sa.Text(), nullable=True))
    # The name is what app/switch.py matches on to tell a replay from a
    # generated PIN/serial collision, so it must stay in step with the
    # metadata naming convention (uq_<table>_<column>).
    op.create_unique_constraint(
        op.f("uq_vouchers_idempotency_key"), "vouchers", ["idempotency_key"]
    )


def downgrade() -> None:
    op.drop_constraint(op.f("uq_vouchers_idempotency_key"), "vouchers", type_="unique")
    op.drop_column("vouchers", "idempotency_key")
