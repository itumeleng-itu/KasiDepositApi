"""Test fixtures. Tests run in their own Postgres schema and nothing else.

- The database is TEST_DATABASE_URL, or DATABASE_URL when that is unset (one
  database may serve both). If neither is set the run aborts.
- Tests live in the schema TEST_DATABASE_SCHEMA (default `kasideposit_test`),
  with their own tables, enum types and migration history.
- If the test database *and* schema are the same as the app's, the run
  aborts: the suite truncates tables, and would wipe seeded demo vouchers.
- Migrations run once per session. Tables are truncated before each test that
  touches the database. The database itself is never dropped or created, since
  a hosted provider may not permit it; only the test schema is created if missing.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.config import ENV_FILE, Settings, normalize_database_url, validate_schema_name
from app.db import make_engine, make_session_factory, same_database
from app.main import create_app
from app.models import Base

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEST_SCHEMA = "kasideposit_test"


class _TestEnv(BaseSettings):
    """Reads the variables the same way the app does (environment, then .env)."""

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    test_database_url: str | None = None
    test_database_schema: str = DEFAULT_TEST_SCHEMA
    database_url: str | None = None
    database_schema: str = "public"


def check_test_database(
    test_url: str | None,
    test_schema: str,
    dev_url: str | None,
    dev_schema: str = "public",
) -> str:
    """Return the database URL to test against, or raise pytest.UsageError to abort."""
    url = test_url or dev_url
    if not url:
        raise pytest.UsageError(
            "Neither TEST_DATABASE_URL nor DATABASE_URL is set; there is no database to test against."
        )
    try:
        validate_schema_name(test_schema)
    except ValueError as exc:
        raise pytest.UsageError(f"TEST_DATABASE_SCHEMA: {exc}") from exc
    if dev_url and same_database(url, dev_url) and test_schema == dev_schema:
        raise pytest.UsageError(
            f"The test schema {test_schema!r} is the app's own schema in the same database. "
            "The test suite truncates tables and would destroy development data. "
            "Refusing to run."
        )
    return normalize_database_url(url)


_TEST_URL_KEY = pytest.StashKey[str]()
_TEST_SCHEMA_KEY = pytest.StashKey[str]()


def pytest_configure(config: pytest.Config) -> None:
    env = _TestEnv()
    config.stash[_TEST_URL_KEY] = check_test_database(
        env.test_database_url, env.test_database_schema, env.database_url, env.database_schema
    )
    config.stash[_TEST_SCHEMA_KEY] = env.test_database_schema


@pytest.fixture(scope="session")
def test_settings(pytestconfig: pytest.Config) -> Settings:
    return Settings(
        database_url=pytestconfig.stash[_TEST_URL_KEY],
        database_schema=pytestconfig.stash[_TEST_SCHEMA_KEY],
        environment="test",
    )


@pytest.fixture(scope="session")
def engine(test_settings: Settings) -> Iterator[Engine]:
    eng = make_engine(test_settings.database_url, test_settings.database_schema)
    with eng.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{test_settings.database_schema}"'))

    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.attributes["configure_logger"] = False
    # configparser treats % as interpolation; escape it (passwords may contain one).
    alembic_cfg.set_main_option(
        "sqlalchemy.url", test_settings.database_url.replace("%", "%%")
    )
    alembic_cfg.set_main_option("kasideposit.schema", test_settings.database_schema)
    command.upgrade(alembic_cfg, "head")

    yield eng
    eng.dispose()


@pytest.fixture
def clean_db(engine: Engine) -> Engine:
    """Truncate every application table in the test schema before the test runs."""
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
