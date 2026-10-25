"""The database enforces the voucher invariants itself, independent of app code.

Rows here are written with raw SQL on purpose: these tests check what
Postgres rejects, not what the application happens to avoid doing.
"""

import uuid
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from app.models import Base, Voucher, VoucherStatus
from app.pii import SA_ID, PiiCipher
from tests.conftest import TEST_PII_KEY

PIN = "1234567890123456"
SERIAL = "20260924123456789012"

INSERT = text(
    "INSERT INTO vouchers (pin, amount_cents, status, redeemed_at, serial)"
    " VALUES (:pin, :amount_cents, :status, :redeemed_at, :serial)"
)


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "pin": PIN,
        "amount_cents": 50000,
        "status": "active",
        "redeemed_at": None,
        "serial": SERIAL,
    }
    row.update(overrides)
    return row


def test_valid_active_and_redeemed_rows_are_accepted(db_session: Session) -> None:
    db_session.execute(INSERT, _row())
    db_session.execute(
        INSERT,
        _row(
            pin="6543210987654321",
            serial="20260924000000000001",
            status="redeemed",
            redeemed_at="2026-09-24T10:00:00+02:00",
        ),
    )
    db_session.commit()
    assert db_session.execute(text("SELECT count(*) FROM vouchers")).scalar_one() == 2


def test_redeemed_row_without_redeemed_at_is_rejected(db_session: Session) -> None:
    with pytest.raises(IntegrityError, match="ck_vouchers_redeemed_at_matches_status"):
        db_session.execute(INSERT, _row(status="redeemed", redeemed_at=None))


def test_active_row_with_redeemed_at_is_rejected(db_session: Session) -> None:
    with pytest.raises(IntegrityError, match="ck_vouchers_redeemed_at_matches_status"):
        db_session.execute(INSERT, _row(redeemed_at="2026-09-24T10:00:00+02:00"))


@pytest.mark.parametrize("amount", [0, -1])
def test_non_positive_amount_is_rejected(db_session: Session, amount: int) -> None:
    with pytest.raises(IntegrityError, match="ck_vouchers_amount_cents_positive"):
        db_session.execute(INSERT, _row(amount_cents=amount))


def test_unknown_status_is_rejected_by_the_enum(db_session: Session) -> None:
    with pytest.raises(DataError, match="voucher_status"):
        db_session.execute(INSERT, _row(status="spent"))


@pytest.mark.parametrize("pin", ["123456789012345", "12345678901234567", "12345678901234ab", ""])
def test_malformed_pin_is_rejected(db_session: Session, pin: str) -> None:
    with pytest.raises(IntegrityError, match="ck_vouchers_pin_format"):
        db_session.execute(INSERT, _row(pin=pin))


def test_malformed_serial_is_rejected(db_session: Session) -> None:
    with pytest.raises(IntegrityError, match="ck_vouchers_serial_format"):
        db_session.execute(INSERT, _row(serial="2026092412345"))


def test_duplicate_pin_is_rejected(db_session: Session) -> None:
    db_session.execute(INSERT, _row())
    with pytest.raises(IntegrityError, match="pk_vouchers"):
        db_session.execute(INSERT, _row(serial="20260924000000000001"))


def test_duplicate_serial_is_rejected(db_session: Session) -> None:
    db_session.execute(INSERT, _row())
    with pytest.raises(IntegrityError, match="uq_vouchers_serial"):
        db_session.execute(INSERT, _row(pin="6543210987654321"))


def test_leading_zero_pin_round_trips_through_the_database(db_session: Session) -> None:
    db_session.execute(INSERT, _row(pin="0000000000000042"))
    db_session.commit()
    voucher = db_session.get(Voucher, "0000000000000042")
    assert voucher is not None
    assert voucher.pin == "0000000000000042"
    assert voucher.status is VoucherStatus.ACTIVE
    assert voucher.redeemed_at is None
    assert voucher.issued_at.tzinfo is not None


def test_active_status_partial_index_exists(db_session: Session) -> None:
    indexdef = db_session.execute(
        text(
            "SELECT indexdef FROM pg_indexes"
            " WHERE schemaname = current_schema() AND indexname = 'ix_vouchers_status_active'"
        )
    ).scalar_one()
    assert "WHERE (status = 'active'::voucher_status)" in indexdef


def test_every_table_has_row_level_security(db_session: Session) -> None:
    # Hosted providers (Supabase) expose tables to a public API key unless RLS
    # is on. Every application table, now and in later phases, must enable it.
    names = [t.name for t in Base.metadata.sorted_tables] + ["alembic_version"]
    rows = db_session.execute(
        text(
            "SELECT c.relname, c.relrowsecurity FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = current_schema() AND c.relname = ANY(:names)"
        ),
        {"names": names},
    ).all()
    assert sorted(r.relname for r in rows) == sorted(names)
    assert [r.relname for r in rows if not r.relrowsecurity] == []


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_hosted_api_roles_have_no_table_privileges(db_session: Session, role: str) -> None:
    if not db_session.execute(
        text("SELECT exists(SELECT 1 FROM pg_roles WHERE rolname = :r)"), {"r": role}
    ).scalar_one():
        pytest.skip(f"no {role!r} role on this Postgres (not a Supabase-style host)")
    for table in [t.name for t in Base.metadata.sorted_tables] + ["alembic_version"]:
        granted = db_session.execute(
            text(
                "SELECT array_remove(ARRAY["
                " CASE WHEN has_table_privilege(:r, :t, 'SELECT') THEN 'SELECT' END,"
                " CASE WHEN has_table_privilege(:r, :t, 'INSERT') THEN 'INSERT' END,"
                " CASE WHEN has_table_privilege(:r, :t, 'UPDATE') THEN 'UPDATE' END,"
                " CASE WHEN has_table_privilege(:r, :t, 'DELETE') THEN 'DELETE' END], NULL)"
            ),
            {"r": role, "t": table},
        ).scalar_one()
        assert granted == [], f"{role} can {granted} on {table}"


def test_models_match_migrations(clean_db: Engine) -> None:
    with clean_db.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_migration_downgrades_cleanly_and_upgrades_again(
    clean_db: Engine, alembic_config: Config
) -> None:
    enums = ("voucher_status", "deposit_status", "ledger_account", "payout_status")
    functions = ("set_updated_at", "ledger_entries_append_only", "ledger_entry_group_balanced")
    type_count = text(
        "SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
        " WHERE t.typname = ANY(:names) AND n.nspname = current_schema()"
    ).bindparams(names=list(enums))
    function_count = text(
        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE p.proname = ANY(:names) AND n.nspname = current_schema()"
    ).bindparams(names=list(functions))
    try:
        command.downgrade(alembic_config, "base")
        with clean_db.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('vouchers')")).scalar_one() is None
            assert conn.execute(type_count).scalar_one() == 0
            assert conn.execute(function_count).scalar_one() == 0
    finally:
        # Always leave the schema at head for the rest of the session.
        command.upgrade(alembic_config, "head")
    with clean_db.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('vouchers')")).scalar_one() is not None
        assert conn.execute(type_count).scalar_one() == len(enums)
        assert conn.execute(function_count).scalar_one() == len(functions)


def test_one_owner_migration_refuses_a_database_with_a_shared_number(
    clean_db: Engine, alembic_config: Config
) -> None:
    """Which user keeps a shared number is a human decision: 0008 must stop, not delete."""
    insert = text(
        "INSERT INTO users (id, full_names, id_number_hash, id_number_encrypted, date_of_birth,"
        " status, verification_ref, verified_at) VALUES (:id, 'Thabo Mokoena', :hash, :encrypted,"
        " '1980-01-01', 'active', 'test', now())"
    )
    method = text(
        "INSERT INTO payout_methods (id, user_id, kind, bank_id, shap_id, shap_name, verification_ref,"
        " verified_at, is_default) VALUES (:id, :user_id, 'shap_id', 'capitec', '+27825551234',"
        " 'T. Mokoena', 'test', now(), true)"
    )
    try:
        command.downgrade(alembic_config, "0007_vouchers_idempotency_key")
        with clean_db.begin() as conn:
            for n in (1, 2):
                user_id = uuid.uuid4()
                pii, id_number = PiiCipher(TEST_PII_KEY), f"800101500{n}087"
                conn.execute(
                    insert,
                    {
                        "id": user_id,
                        "hash": pii.lookup_hash(id_number, SA_ID),
                        "encrypted": pii.encrypt(id_number, SA_ID),
                    },
                )
                conn.execute(method, {"id": uuid.uuid4(), "user_id": user_id})
        with pytest.raises(RuntimeError, match="1 PayShap number"):
            command.upgrade(alembic_config, "head")
        with clean_db.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('uq_payout_methods_shap_id')")).scalar_one() is None
            assert conn.execute(text("SELECT count(*) FROM payout_methods")).scalar_one() == 2
    finally:
        with clean_db.begin() as conn:
            conn.execute(text("TRUNCATE payout_methods, user_sessions, users CASCADE"))
        command.upgrade(alembic_config, "head")
