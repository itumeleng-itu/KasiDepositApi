# KasiDeposit API

Backend for KasiDeposit: a person hands cash to a spaza shop, receives a
retail voucher PIN, and redeems it into their own bank account.

## The system

**One backend, one database, two surfaces.**

| Surface | What it is | Status |
|---|---|---|
| `/demo/*` | Stands in for a voucher issuer's vending terminal. It exists only so the product can be demonstrated. Absent in production. **The mobile app must never call it.** | Built |
| `/v1/*` | The mobile app's API: look up a voucher, create a deposit, poll its status, resolve a ShapID. | **None of this exists yet** |

`GET /health` sits outside both and reports only whether the database is reachable.

**Vouchers are single-use and full-value.** There is no partial redemption and
no change PIN. The app never sends an amount: the backend reads the voucher's
amount from its row and pays out all of it. **Any endpoint that accepts an
amount from the client is wrong.** The one exception is `POST /demo/vouchers`,
which *creates* a voucher for an amount, the way the issuer's till does.

All voucher logic sits behind `VoucherSwitch` (`app/switch.py`). In
production that is a third party's switch reached over HTTP; our database is
the demo standing in for it, so a real issuer replaces one class.

### `/demo` safeguards

- On by default only when `ENVIRONMENT=development`. With
  `ENABLE_DEMO_ROUTES=false` the router isn't mounted at all, so `/demo`
  returns 404.
- The app refuses to start with `ENVIRONMENT=production` and `ENABLE_DEMO_ROUTES=true`.
- Every `/demo` response carries `X-Demo-Surface: true`.

### One database, two schemas

`DATABASE_URL` points at the one Supabase database. Inside it:

| Schema | Setting (default) | Used when |
|---|---|---|
| App data | `DEV_SCHEMA` (`public`) | `ENVIRONMENT=development` or `production` |
| Test data | `TEST_SCHEMA` (`kd_test`) | `ENVIRONMENT=test`, which the test suite always uses |

Alembic migrates the schema that `ENVIRONMENT` selects. Each schema keeps its
own `alembic_version` table.

The test suite truncates tables. It refuses to run if `TEST_SCHEMA` equals
`DEV_SCHEMA`, because that would destroy the seeded demo vouchers. It also
refuses to start while another test run is using `TEST_SCHEMA`: two runs
truncating one schema delete each other's rows mid-test.

### Timeouts

The demo runs on venue wifi, so every database connection (app, tests and
Alembic) fails fast rather than hanging:

| Setting | Value |
|---|---|
| Connect timeout | 10s |
| `statement_timeout` | 30s |
| `lock_timeout` | 5s |
| `idle_in_transaction_session_timeout` | 10s |
| TCP keepalives | On, so a connection whose network has dropped is noticed |

`GET /health` returns `503 {"status": "error", "database": "unreachable"}`
within the connect timeout when the database can't be reached. If a
connection opens but then stalls, a hard 15s deadline (the connect timeout
plus a 5s query budget) still answers 503. It never hangs.

## Setup

Requires Python 3.11+ and a Postgres database. You create the database; the
service never creates it.

```sh
python -m venv .venv
.venv\Scripts\activate            # Windows; on macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env              # then fill in DATABASE_URL and ENVIRONMENT
```

For Supabase, use the **Session pooler** connection string (see `.env.example`).

**Windows:**

- Run tools as `python -m <tool>`, as below. Smart App Control blocks the
  unsigned launcher `.exe` files that pip generates (`uvicorn.exe`, `alembic.exe` and so on).
- `psycopg` is installed without its bundled binary, whose DLLs Smart App
  Control also blocks. It loads the system `libpq` instead: install the
  PostgreSQL command-line tools (EDB installer, "Command Line Tools" only)
  and put their `bin` folder on `PATH`.

## Migrate, seed, run

```sh
python -m alembic upgrade head     # migrates DEV_SCHEMA (ENVIRONMENT=development)
python seed.py --count 20          # prints PINs, amounts and status
python -m uvicorn app.main:create_app --factory --reload
```

`python seed.py --reset --yes` clears all vouchers first; without `--yes` it
only reports what it would delete. It refuses to run unless
`ENVIRONMENT=development`.

Then try `GET /health`, `POST /demo/vouchers` with `{"amount_cents": 50000}`,
`GET /demo/vouchers`, and the docs at `/docs`.

## Tests

```sh
python -m pytest
```

The suite migrates `TEST_SCHEMA` itself and never touches `DEV_SCHEMA`.

## Conventions

- Money is integer cents (`app/money.py`). No floats, no `Decimal` in the database.
- Schema changes are Alembic migrations, never `create_all`.
- Every table must enable row-level security (see migration `0003`): hosted
  providers expose `public` to a public API key otherwise. A test enforces it.

## Known tech debt

- The four check constraints on `vouchers` (migration `0001`) have a doubled name prefix, such as `ck_vouchers_ck_vouchers_pin_format`. They work; the names are cosmetic, and renaming them isn't worth a migration before the demo.
