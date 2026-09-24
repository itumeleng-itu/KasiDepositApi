# KasiDeposit API

Backend for KasiDeposit: a person hands cash to a spaza shop, receives a
retail voucher PIN, and redeems it into their own bank account.

This phase covers the foundation and voucher **vending**. Redemption, the
ledger and payouts come later. Vouchers are single-use and full-value: a
voucher is redeemed for its whole amount or not at all.

## Vending is demo scaffolding

In the real world the spaza's own terminal issues the voucher, through a
third-party issuer's switch. The `/demo` endpoints stand in for that
terminal so the product can be demonstrated end to end. All voucher logic
sits behind `VoucherSwitch` (`app/switch.py`), so a real issuer replaces
one class.

**The mobile app never touches `/demo`.** It never vends. The demo routes
are on by default only in development, are absent entirely when
`ENABLE_DEMO_ROUTES=false`, and the app refuses to start in production with
them on. Every `/demo` response carries `X-Demo-Surface: true`.

## Setup

Requires Python 3.11+ and a Postgres database. You create the database; the
service never creates it.

```sh
python -m venv .venv
.venv\Scripts\activate            # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # then fill in DATABASE_URL and ENVIRONMENT
```

**libpq:** `psycopg` is installed without its bundled binary, and loads the
system `libpq`. On Windows, install the PostgreSQL command-line tools (EDB
installer, "Command Line Tools" only) and put its `bin` folder on `PATH`.
On macOS/Linux, install your package manager's `libpq`.

### Databases

`DATABASE_URL` is your development database. For a local Postgres:

```sh
createdb kasideposit
```

For Supabase, use the **Session pooler** connection string (see `.env.example`).

Tests need no second database. They run in their own schema
(`kasideposit_test`) and never touch the app's tables. If you do set
`TEST_DATABASE_URL` to a separate database, **it must not be the same
database as `DATABASE_URL`** unless the schemas differ: the suite truncates
tables, and the run aborts rather than destroy development data.

### Migrate, seed, run

```sh
alembic upgrade head
python seed.py --count 20          # prints PINs, amounts and status
uvicorn app.main:create_app --factory --reload
```

`python seed.py --reset --yes` clears all vouchers first; without `--yes` it
only reports what it would delete. It refuses to run unless
`ENVIRONMENT=development`.

Then: `GET /health`, `POST /demo/vouchers` with `{"amount_cents": 50000}`,
`GET /demo/vouchers`, and the docs at `/docs`.

## Tests

```sh
pytest
```

## Conventions

- Money is integer cents (`app/money.py`). No floats, no `Decimal` in the database.
- Schema changes are Alembic migrations, never `create_all`.
- Every table must enable row-level security (see migration `0003`): hosted
  providers expose `public` to a public API key otherwise. A test enforces it.
