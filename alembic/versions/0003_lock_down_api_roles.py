"""lock vouchers and alembic_version away from hosted-API roles

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-24

Hosted Postgres providers such as Supabase expose the `public` schema over
an HTTP API to the `anon` and `authenticated` roles, and grant those roles
full privileges on every new table by default. The anon key is public by
design, so without this anyone could read every voucher PIN, or vend and
redeem vouchers directly, bypassing the switch.

- Row-level security is enabled with no policies: API roles see no rows.
  The app connects as the table owner, which RLS does not restrict.
- Privileges are revoked from `anon` and `authenticated` where those roles
  exist, so this is a no-op on plain Postgres.

EVERY FUTURE TABLE needs the same treatment (tests/test_schema.py enforces
RLS on all application tables).

The downgrade disables RLS but deliberately does not re-grant privileges:
reopening PINs to a public API key is never a step to automate.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("vouchers", "alembic_version")
API_ROLES = ("anon", "authenticated")


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    for role in API_ROLES:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    REVOKE ALL ON {", ".join(TABLES)} FROM {role};
                END IF;
            END
            $$
            """
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
