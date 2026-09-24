import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, text

from app.config import Settings, normalize_database_url
from app.db import same_database
from tests.conftest import DEFAULT_TEST_SCHEMA, check_test_database

DEV = "postgresql://kd:secret@localhost:5432/kasideposit"
TEST = "postgresql://kd:secret@localhost:5432/kasideposit_test"


def test_guard_aborts_when_test_url_equals_database_url_and_schema() -> None:
    with pytest.raises(pytest.UsageError, match="destroy development data"):
        check_test_database(DEV, "public", DEV, "public")


def test_guard_sees_through_driver_and_password_differences() -> None:
    disguised = "postgresql+psycopg://kd:other@LOCALHOST/kasideposit?sslmode=require"
    with pytest.raises(pytest.UsageError, match="destroy development data"):
        check_test_database(disguised, "public", DEV, "public")


def test_guard_allows_same_database_in_a_separate_schema() -> None:
    assert check_test_database(DEV, DEFAULT_TEST_SCHEMA, DEV, "public").startswith(
        "postgresql+psycopg://"
    )


def test_guard_falls_back_to_database_url_only_with_a_separate_schema() -> None:
    assert check_test_database(None, DEFAULT_TEST_SCHEMA, DEV) == normalize_database_url(DEV)
    with pytest.raises(pytest.UsageError, match="destroy development data"):
        check_test_database(None, "public", DEV)


def test_guard_aborts_with_no_database_at_all() -> None:
    with pytest.raises(pytest.UsageError, match="no database to test against"):
        check_test_database(None, DEFAULT_TEST_SCHEMA, None)


def test_guard_rejects_unsafe_schema_name() -> None:
    with pytest.raises(pytest.UsageError, match="TEST_DATABASE_SCHEMA"):
        check_test_database(TEST, 'x"; drop schema public; --', DEV)


def test_guard_passes_distinct_databases() -> None:
    assert check_test_database(TEST, "public", DEV).startswith("postgresql+psycopg://")


def test_same_database_distinguishes_pooler_tenants() -> None:
    pooler = "postgresql://postgres.{ref}:pw@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"
    assert not same_database(pooler.format(ref="aaa"), pooler.format(ref="bbb"))


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


def test_connections_are_pinned_to_the_test_schema(
    clean_db: Engine, test_settings: Settings
) -> None:
    with clean_db.connect() as conn:
        current = conn.execute(text("SELECT current_schema()")).scalar_one()
    assert current == test_settings.database_schema
