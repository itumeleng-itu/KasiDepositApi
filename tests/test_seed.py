import io

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import make_session_factory
from app.models import Base, Voucher, VoucherStatus
from app.money import rand_to_cents
from seed import DEMO_AMOUNTS_RAND, RESET_TABLES, parse_args, run


@pytest.fixture
def dev_settings(test_settings: Settings) -> Settings:
    # Development rules, but every schema still points at TEST_SCHEMA: switching
    # ENVIRONMENT alone would select DEV_SCHEMA, the seeded demo data.
    return test_settings.model_copy(
        update={"environment": "development", "dev_schema": test_settings.test_schema}
    )


def _count(engine: Engine) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(Voucher)) or 0


def test_seed_vends_the_requested_count(clean_db: Engine, dev_settings: Settings) -> None:
    out = io.StringIO()
    assert run(parse_args(["--count", "5"]), dev_settings, make_session_factory(clean_db), out) == 0

    with Session(clean_db) as session:
        vouchers = session.scalars(select(Voucher)).all()
    assert len(vouchers) == 5
    allowed = {rand_to_cents(r) for r in DEMO_AMOUNTS_RAND}
    assert all(v.amount_cents in allowed and v.status is VoucherStatus.ACTIVE for v in vouchers)
    printed = out.getvalue()
    assert all(v.pin in printed for v in vouchers)
    assert "5 voucher(s)" in printed


@pytest.mark.parametrize("environment", ["test", "production"])
def test_seed_refuses_outside_development(
    clean_db: Engine, test_settings: Settings, environment: str
) -> None:
    settings = test_settings.model_copy(
        update={
            "environment": environment,
            "enable_demo_routes": False,
            "dev_schema": test_settings.test_schema,
        }
    )
    out = io.StringIO()
    assert run(parse_args(["--count", "3"]), settings, make_session_factory(clean_db), out) == 1
    assert "Refusing to seed" in out.getvalue()
    assert _count(clean_db) == 0


def test_reset_without_yes_deletes_nothing(clean_db: Engine, dev_settings: Settings) -> None:
    factory = make_session_factory(clean_db)
    assert run(parse_args(["--count", "2"]), dev_settings, factory, io.StringIO()) == 0
    assert _count(clean_db) == 2

    out = io.StringIO()
    assert run(parse_args(["--reset", "--count", "3"]), dev_settings, factory, out) == 1
    assert "would delete all 2 voucher(s)" in out.getvalue()
    assert _count(clean_db) == 2


def test_reset_with_yes_replaces_the_vouchers(clean_db: Engine, dev_settings: Settings) -> None:
    factory = make_session_factory(clean_db)
    assert run(parse_args(["--count", "2"]), dev_settings, factory, io.StringIO()) == 0
    assert _count(clean_db) == 2

    out = io.StringIO()
    assert run(parse_args(["--reset", "--yes", "--count", "3"]), dev_settings, factory, out) == 0
    assert "Deleted 2 voucher(s)" in out.getvalue()
    assert _count(clean_db) == 3


@pytest.mark.parametrize("count", ["0", "-1", "1001"])
def test_count_is_bounded(count: str) -> None:
    with pytest.raises(SystemExit):
        parse_args(["--count", count])


def test_reset_covers_every_table() -> None:
    # There is no CASCADE: a new table must be added to RESET_TABLES deliberately.
    assert {m.__tablename__ for m in RESET_TABLES} == set(Base.metadata.tables)
