"""The database enforces the voucher invariants itself, independent of app code.

Rows here are written with raw SQL on purpose: these tests check what
Postgres rejects, not what the application happens to avoid doing.
"""

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


def test_models_match_migrations(clean_db: Engine) -> None:
    with clean_db.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_migration_downgrades_cleanly_and_upgrades_again(
    clean_db: Engine, alembic_config: Config
) -> None:
    type_count = text(
        "SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
        " WHERE t.typname = 'voucher_status' AND n.nspname = current_schema()"
    )
    try:
        command.downgrade(alembic_config, "base")
        with clean_db.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('vouchers')")).scalar_one() is None
            assert conn.execute(type_count).scalar_one() == 0
    finally:
        # Always leave the schema at head for the rest of the session.
        command.upgrade(alembic_config, "head")
    with clean_db.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('vouchers')")).scalar_one() is not None
        assert conn.execute(type_count).scalar_one() == 1
