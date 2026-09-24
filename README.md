# KasiDeposit API

Backend for KasiDeposit: a person hands cash to a spaza shop, receives a
retail voucher PIN, and redeems it into their own bank account.

## The money story

A person hands R500 cash to a spaza shop and receives a voucher PIN. That
cash is now the **shop's**, and reaches the voucher issuer through the
issuer's own settlement cycle, not through us.

When she redeems the PIN with us, we owe her R495 (R500 less our R5 fee)
**immediately**, but the issuer has not paid us yet. So the R495 goes out of
a **prefunded settlement account** we control at our sponsor bank (our
float), and the issuer reimburses us later.

So we do hold funds, briefly. We pay out of our own float and are reimbursed
afterwards. Three things follow, and the code reflects each:

1. **We carry the gap.** What the issuer owes us is `voucher_receivable`.
2. **The float is finite.** A payout the float cannot cover is refused as
   `insufficient_float` before the voucher is charged, so nothing is lost and
   "try again later" is true.
3. **Every cent is traceable.** A double-entry ledger, append-only, with no
   stored balances (`app/ledger.py` explains the accounts and signs).

A successful R500 deposit, in the ledger (debits positive, credits negative):

| When | Account | Amount | Meaning |
|---|---|---|---|
| Charged | `voucher_receivable` | +R500 | the issuer owes us R500 |
| | `user_payable` | −R495 | we owe her R495 |
| | `fee_income` | −R5 | we earned R5 |
| Payout settled | `user_payable` | +R495 | we no longer owe her |
| | `settlement` | −R495 | the money left our float |

The float is topped up from `capital` (`python seed.py --fund 10000`).

## The system

**One backend, one database, two surfaces.**

| Surface | What it is | Status |
|---|---|---|
| `/demo/*` | Stands in for a voucher issuer's vending terminal, plus the float. It exists only so the product can be demonstrated. Hosted, it sits behind a secret key. **The mobile app must never call it.** | Built |
| `/v1/*` | The mobile app's API: look up a voucher, resolve a ShapID, create a deposit, poll its status. | Built |

`GET /health` sits outside both and reports only whether the database is reachable.

**Vouchers are single-use and full-value.** There is no partial redemption and
no change PIN. The app never sends an amount: the backend reads the voucher's
amount from its row and pays out all of it, less the fee. **Any endpoint that
accepts an amount from the client is wrong.** The one exception is
`POST /demo/vouchers`, which *creates* a voucher for an amount, the way the
issuer's till does.

Three seams stand in for third parties, each replaceable by one class:

| Seam | Stands in for | File |
|---|---|---|
| `VoucherSwitch` | the voucher issuer's switch | `app/switch.py` |
| `PayoutProvider` (mock) | a payout API such as Stitch or Peach | `app/payouts.py` |
| ShapID resolution (mock) | PayShap's proxy directory | `app/shapid.py` |

### `/v1`: the mobile app's API

It matches the app's `src/api/wire.ts` exactly, in snake_case. The app's base
URL includes `/v1`.

| Call | Body | Returns |
|---|---|---|
| `POST /v1/vouchers/lookup` | `{pin}` | `{voucher_token, value_cents, fee_cents, payout_cents}` |
| `GET /v1/shapid/{shap_id}` | (URL-encoded) | `{shap_name, bank}` |
| `POST /v1/deposits` | `{voucher_token, destination}` + `Idempotency-Key` header | `{id, reference, status, payout_cents, failure_reason}`: 201 new, 200 replay |
| `GET /v1/deposits/{id}` | | the same deposit shape |

- The PIN is sent once, at lookup. After that the app uses the voucher token,
  which is opaque, single-use and expires after 10 minutes.
- Refusals are `{"reason": "..."}` with the app's exact strings, for example
  `voucher_already_redeemed` or `insufficient_float`.
- The same `Idempotency-Key` returns the same deposit. The database enforces
  this, not a pre-check.
- The app sees four statuses: `pending`, `submitted`, `completed` and
  `failed`. Internally a deposit is `pending`, `charged`, `paying`, `settled`
  or `failed` (`app/deposits.py`).
- No `/v1` response carries `X-Demo-Surface`.

### What happens on a deposit

1. **Resolve the destination first.** A ShapID failure means nothing was charged.
2. **In one transaction:** check the float, charge the voucher under a row
   lock, record the deposit, post the charge entries, then commit. Two
   requests for one PIN cannot both succeed, and neither can two deposits
   that together exceed the float.
3. **Instruct the payout.** If the provider cannot be reached, the deposit
   stays charged and the next status read instructs it.
4. **The app polls.** Each read moves an in-flight deposit forward. A
   completed payout posts the settlement entries. A failed payout reverses the
   charge and returns the voucher to active, so "Try again" with the same PIN
   works. A slow or unreachable provider is never a failure.

Reversing a charge is a **demo simplification**: a real issuer would need a
reversal API, and may not expose one.

### Demo scenarios

| To show | Do |
|---|---|
| A normal deposit | Any voucher, any ShapID not listed below |
| `bank_processing_error` | A R405 voucher (payout R400.00) |
| `limit_exceeded` | A R406 voucher (payout R401.00) |
| `bank_unavailable` | A R407 voucher (payout R402.00) |
| `insufficient_float` | `seed.py --reset --yes` then no `--fund`, or a small `--fund` |
| ShapID not found / suspended / ambiguous | A number ending in 9 / 8 / 7 (7 resolves with `@bank`) |

The mock payout moves to `submitted` after about 1.5s and settles after 3s.
Only the three amounts above fail; every other payout completes. The ShapID
scenarios match the app's own fake (see its `TESTING.md`).

### `/demo`: the till and the float

| Call | Does |
|---|---|
| `POST /demo/vouchers` `{"amount_cents": 50000}` | vends a voucher, as the spaza's till would |
| `GET /demo/vouchers` | the 50 most recent vouchers, PINs masked |
| `GET /demo/float` | the float: settlement balance, and what is still free to pay out |
| `POST /demo/float` `{"amount_cents": 1000000}` | tops up the float from capital (up to R1 000 000 at a time) |

Safeguards:

- **Off unless switched on.** On by default only when `ENVIRONMENT=development`.
  With `ENABLE_DEMO_ROUTES=false` the router isn't mounted at all.
- **Behind a key when hosted.** With `DEMO_API_KEY` set (at least 32
  characters), every `/demo` request must send it as `X-Demo-Key`. Without it,
  `/demo` answers a plain 404, even for a malformed body, exactly like a path
  that doesn't exist. It is also left out of the OpenAPI schema. Production
  refuses to start with demo routes on and no key.
- **Marked.** Every authorised `/demo` response carries `X-Demo-Surface: true`.

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
python -m alembic upgrade head                 # migrates DEV_SCHEMA (ENVIRONMENT=development)
python seed.py --fund 10000 --count 20         # R10 000 of float, 20 vouchers; prints the PINs
python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
```

- **Resetting:** `python seed.py --reset --yes` clears all demo data, float
  included. Without `--yes` it only reports what it would delete.
- **Environment:** `seed.py` refuses to run unless `ENVIRONMENT=development`.
- **`--host 0.0.0.0`:** lets a phone on the same wifi reach the API. In the
  app's `.env`, set `EXPO_PUBLIC_USE_FAKE_API=false` and
  `EXPO_PUBLIC_API_BASE_URL=http://<this PC's wifi IP>:8000/v1`.

Then try `GET /health`, vend at `POST /demo/vouchers` with
`{"amount_cents": 50000}`, and see the docs at `/docs`.

## Using the till

The till is a page for a cashier, at **`/till`** (for example
`https://kasidepositapi.onrender.com/till`). It works on a phone, tablet or
computer, and needs no technical knowledge:

1. **First use on a device:** enter the **till code**, which is the server's
   `DEMO_API_KEY`. The browser remembers it, so the cashier doesn't see it again.
2. **Selling:** take the cash, tap **R50, R100, R200, R500 or R1 000** (or type
   another amount in rand), then confirm.
3. **The slip:** shows the **PIN** in large grouped digits, with the amount,
   serial and time. **Print** sends just the slip to a printer.
4. **Manager:** shows the float available to pay out, and tops it up.

The page holds no secret. Everything it does is a `/demo` request carrying the
till code, so without the code it can't create vouchers or touch the float.
It is served only when the demo routes are on, and never appears in the API
schema. The customer deposits the PIN in the mobile app, which never uses the
till code.

## Deploying on Render

`render.yaml` is a Render Blueprint. In the Render dashboard choose
**New > Blueprint** and pick this repo, then set `DATABASE_URL`. Render
generates `DEMO_API_KEY` itself.

| Setting | Value |
|---|---|
| Region | Frankfurt, next to the Supabase database (`eu-central-1`) |
| Plan | Free. It sleeps after 15 minutes without traffic and takes about a minute to wake, longer than the app's 15-second timeout: open `/health` before a demo, or ping it every ~10 minutes to keep it awake. |
| Branch | `main` |
| Build | `pip install -r requirements.txt`, which gets `psycopg[binary]` on Linux |
| Migrations | Not run on deploy (the free plan has no pre-deploy command). Run `python -m alembic upgrade head` from your machine before deploying a version that adds one; startup refuses an out-of-date schema. |
| Start | `python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips "*"` |
| Health check | `/health` |

**Environment variables:**

| Variable | Value | Notes |
|---|---|---|
| `ENVIRONMENT` | `production` | set by `render.yaml` |
| `DATABASE_URL` | the Supabase **Session pooler** string | enter it in the dashboard; percent-encode special characters in the password (`@` becomes `%40`) |
| `ENABLE_DEMO_ROUTES` | `true` | set by `render.yaml` |
| `DEMO_API_KEY` | generated | read it from the dashboard to call `/demo` |

**What production serves:**

- `/health` and `/v1`, open to the app.
- `/demo`, only with the key.
- No `/docs`, `/redoc` or `/openapi.json`.
- Startup refuses a database schema that is behind the code, and says to migrate.

**Operating it from anywhere:**

```sh
curl https://<service>.onrender.com/demo/float -H "X-Demo-Key: $KEY"
curl -X POST https://<service>.onrender.com/demo/float -H "X-Demo-Key: $KEY"      -H "content-type: application/json" -d '{"amount_cents": 1000000}'
curl -X POST https://<service>.onrender.com/demo/vouchers -H "X-Demo-Key: $KEY"      -H "content-type: application/json" -d '{"amount_cents": 50000}'
```

In the mobile app's `.env`, set
`EXPO_PUBLIC_API_BASE_URL=https://<service>.onrender.com/v1`.

## Tests

```sh
python -m pytest
```

The suite migrates `TEST_SCHEMA` itself and never touches `DEV_SCHEMA`. It
includes the concurrency tests: two threads racing one PIN, and two deposits
racing one float, on real separate connections. It runs in about 17 minutes
against a Frankfurt database, because most tests make many network round trips.

## Conventions

- Money is integer cents (`app/money.py`). No floats, no `Decimal` in the database.
- Schema changes are Alembic migrations, never `create_all`.
- Every table must enable row-level security (see migrations `0003` and `0004`):
  hosted providers expose `public` to a public API key otherwise. A test enforces it.
- Lock order is always: the float lock, then the voucher's row lock. Charge the
  voucher before inserting anything that references it.

## Known limits

- **No user authentication or rate limiting on `/v1`.** Anyone can look up a
  PIN (it takes the full 16 digits, and there are 10^16 of them), and every
  lookup stores a short-lived token.
- **Payouts advance when the deposit is read.** There is no background worker,
  so a deposit nobody polls stays `submitted` until someone reads it.
- **The float lock serialises every deposit.** That is fine at demo volume.
- **The account-number destination** (`kind: "account"`) stores a bank account
  number. That is personal information under POPIA. Nothing constructs it
  today, and it needs a retention and encryption decision before it goes live.
- **Logs:** ShapIDs are masked in the access log (`/v1/shapid/***`), and SQL
  errors never include their parameters, so PINs stay out of logs.
- **A real payout provider** must be called outside the database transaction.
  The mock answers instantly; a slow real call would hit the 10s
  idle-in-transaction limit.

## Known tech debt

- The four check constraints on `vouchers` (migration `0001`) have a doubled name prefix, such as `ck_vouchers_ck_vouchers_pin_format`. They work; the names are cosmetic, and renaming them isn't worth a migration before the demo.
