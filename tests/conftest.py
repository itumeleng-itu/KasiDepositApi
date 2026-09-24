"""Test fixtures. One database, and the tests work only in TEST_SCHEMA.

- Settings come from the environment with ENVIRONMENT forced to "test", so
  every connection's search_path is TEST_SCHEMA (default `kd_test`).
- The run aborts if TEST_SCHEMA equals DEV_SCHEMA: the suite truncates tables
  and would destroy the seeded demo vouchers.
- Only one test run may use TEST_SCHEMA at a time. The suite truncates before
  every database test, so two concurrent runs silently delete each other's
  rows mid-test and fail at random. A Postgres advisory lock, held for the
  whole session, makes a second run abort at once instead.
- Migrations run once per session. The database itself is never dropped or
  created (a hosted provider may not permit it); only TEST_SCHEMA is created
  if missing.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Connection, Engine, func, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import make_engine, make_session_factory
from app.main import create_app
from app.models import Base, LedgerEntry

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def check_schemas(test_schema: str, dev_schema: str) -> None:
    """Abort the run (pytest.UsageError) if tests would run in the app's schema."""
    if test_schema == dev_schema:
        raise pytest.UsageError(
            f"TEST_SCHEMA and DEV_SCHEMA are both {test_schema!r}. "
            "The test suite truncates tables and would destroy the seeded demo vouchers. "
            "Refusing to run."
        )


def load_test_settings() -> Settings:
    try:
        settings = Settings(
            environment="test",
            # Off by default outside development; the route tests need them on.
            enable_demo_routes=True,
            min_voucher_cents=1000,
            max_voucher_cents=500000,
        )
    except ValidationError as exc:
        raise pytest.UsageError(f"Cannot load settings for the test run:\n{exc}") from exc
    check_schemas(settings.test_schema, settings.dev_schema)
    return settings


_SETTINGS_KEY = pytest.StashKey[Settings]()


def pytest_configure(config: pytest.Config) -> None:
    config.stash[_SETTINGS_KEY] = load_test_settings()


@pytest.fixture(scope="session")
def test_settings(pytestconfig: pytest.Config) -> Settings:
    return pytestconfig.stash[_SETTINGS_KEY]


@pytest.fixture(scope="session")
def alembic_config(test_settings: Settings) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    cfg.attributes["settings"] = test_settings
    return cfg


RUN_LOCK_SQL = text("SELECT pg_try_advisory_lock(hashtext('kasideposit-tests:' || :schema))")


@pytest.fixture(scope="session")
def exclusive_test_run(test_settings: Settings) -> Iterator[Connection]:
    """Hold a session-level advisory lock on TEST_SCHEMA for the whole run.

    The lock is taken outside any transaction, so the connection sits idle
    (not idle-in-transaction) and the idle-transaction timeout never ends it.
    """
    eng = make_engine(test_settings.database_url, test_settings.active_schema)
    conn = eng.connect()
    acquired = conn.execute(RUN_LOCK_SQL, {"schema": test_settings.active_schema}).scalar_one()
    conn.commit()
    if not acquired:
        conn.close()
        eng.dispose()
        pytest.exit(
            f"Another test run is already using TEST_SCHEMA {test_settings.active_schema!r}. "
            "Two runs truncating the same schema delete each other's rows mid-test. "
            "Wait for it to finish, or give this run its own TEST_SCHEMA.",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )
    yield conn
    conn.close()
    eng.dispose()


@pytest.fixture(scope="session")
def engine(
    test_settings: Settings, alembic_config: Config, exclusive_test_run: Connection
) -> Iterator[Engine]:
    eng = make_engine(test_settings.database_url, test_settings.active_schema)
    with eng.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{test_settings.active_schema}"'))
    command.upgrade(alembic_config, "head")

    yield eng
    eng.dispose()


@pytest.fixture
def clean_db(engine: Engine) -> Engine:
    """Truncate every application table in TEST_SCHEMA before the test runs."""
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    if tables:
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return engine


@pytest.fixture
def db_session(clean_db: Engine) -> Iterator[Session]:
    with make_session_factory(clean_db)() as session:
        yield session


@pytest.fixture
def client(clean_db: Engine, test_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(test_settings)) as c:
        yield c


def assert_ledger_balanced(session: Session) -> None:
    """The whole ledger sums to zero, and so does every entry_group in it."""
    total = session.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)))
    assert total == 0
    unbalanced = session.execute(
        select(LedgerEntry.entry_group)
        .group_by(LedgerEntry.entry_group)
        .having(func.sum(LedgerEntry.amount_cents) != 0)
    ).all()
    assert unbalanced == []
