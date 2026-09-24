import os
import subprocess
import sys

import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, Engine, text

from app.config import Settings, normalize_database_url
from tests.conftest import PROJECT_ROOT, check_schemas

DEV = "postgresql://kd:secret@localhost:5432/kasideposit"


def test_guard_aborts_when_test_schema_equals_dev_schema() -> None:
    with pytest.raises(pytest.UsageError) as excinfo:
        check_schemas("public", "public")
    assert str(excinfo.value) == (
        "TEST_SCHEMA and DEV_SCHEMA are both 'public'. "
        "The test suite truncates tables and would destroy the seeded demo vouchers. "
        "Refusing to run."
    )


def test_guard_allows_distinct_schemas() -> None:
    check_schemas("kd_test", "public")


def _run_pytest(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *args],
        cwd=PROJECT_ROOT,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_conftest_refuses_to_run_when_schemas_match() -> None:
    # A real pytest run, so this checks the wiring, not just the helper.
    result = _run_pytest("tests/test_money.py", TEST_SCHEMA="public", DEV_SCHEMA="public")
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "would destroy the seeded demo vouchers" in result.stderr


def test_a_second_concurrent_run_is_refused(exclusive_test_run: Connection) -> None:
    # This run holds the TEST_SCHEMA lock; a second run must stop, not interleave.
    result = _run_pytest("tests/test_health.py::test_health_ok")
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "Another test run is already using TEST_SCHEMA" in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("environment", "expected"),
    [("development", "public"), ("production", "public"), ("test", "kd_test")],
)
def test_environment_picks_the_schema(
    monkeypatch: pytest.MonkeyPatch, environment: str, expected: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", DEV)
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.delenv("DEV_SCHEMA", raising=False)
    monkeypatch.delenv("TEST_SCHEMA", raising=False)
    assert Settings(_env_file=None).active_schema == expected


@pytest.mark.parametrize("variable", ["DEV_SCHEMA", "TEST_SCHEMA"])
def test_schema_names_must_be_plain_identifiers(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", DEV)
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv(variable, 'x"; drop schema public; --')
    with pytest.raises(ValidationError, match="invalid schema name"):
        Settings(_env_file=None)


def test_normalize_database_url() -> None:
    assert normalize_database_url("postgres://u@h/d") == "postgresql+psycopg://u@h/d"
    assert normalize_database_url("postgresql://u@h/d") == "postgresql+psycopg://u@h/d"
    assert (
        normalize_database_url("postgresql+psycopg://u@h/d")
        == "postgresql+psycopg://u@h/d"
    )


def test_unencoded_at_sign_in_password_is_rejected_with_a_hint() -> None:
    with pytest.raises(ValueError, match="%40"):
        normalize_database_url("postgresql://u:p@ss@host:5432/db")
    assert normalize_database_url("postgresql://u:p%40ss@host:5432/db").endswith(
        "u:p%40ss@host:5432/db"
    )


def test_settings_require_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "development")
    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)


def test_settings_reject_empty_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ENVIRONMENT", "development")
    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)


def test_settings_errors_do_not_echo_the_connection_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:hunter2@h/d")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert "hunter2" not in str(excinfo.value)


def test_settings_reject_unknown_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DEV)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    with pytest.raises(ValidationError, match="environment"):
        Settings(_env_file=None)


def test_every_connection_is_set_up_for_the_test_schema_with_timeouts(
    clean_db: Engine, test_settings: Settings
) -> None:
    with clean_db.connect() as conn:
        row = conn.execute(
            text(
                "SELECT current_schema(), current_setting('statement_timeout'),"
                " current_setting('lock_timeout'),"
                " current_setting('idle_in_transaction_session_timeout')"
            )
        ).one()
    assert tuple(row) == (test_settings.test_schema, "30s", "5s", "10s")


@pytest.mark.parametrize(
    ("environment", "expected"),
    [("development", True), ("test", False), ("production", False)],
)
def test_demo_routes_default_on_only_in_development(
    monkeypatch: pytest.MonkeyPatch, environment: str, expected: bool
) -> None:
    monkeypatch.delenv("ENABLE_DEMO_ROUTES", raising=False)
    monkeypatch.setenv("DATABASE_URL", DEV)
    monkeypatch.setenv("ENVIRONMENT", environment)
    assert Settings(_env_file=None).demo_routes_enabled is expected


def test_demo_routes_can_be_switched_off_in_development(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DEV)
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("ENABLE_DEMO_ROUTES", "false")
    assert Settings(_env_file=None).demo_routes_enabled is False


def test_production_refuses_demo_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DEV)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ENABLE_DEMO_ROUTES", "true")
    with pytest.raises(ValidationError, match="must not be true in production"):
        Settings(_env_file=None)


def test_voucher_limits_default_and_must_be_ordered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DEV)
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("MIN_VOUCHER_CENTS", raising=False)
    monkeypatch.delenv("MAX_VOUCHER_CENTS", raising=False)
    settings = Settings(_env_file=None)
    assert (settings.min_voucher_cents, settings.max_voucher_cents) == (1000, 500000)

    monkeypatch.setenv("MIN_VOUCHER_CENTS", "600000")
    with pytest.raises(ValidationError, match="MIN_VOUCHER_CENTS"):
        Settings(_env_file=None)
