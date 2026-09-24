"""What a deployed service needs: a hidden, key-protected /demo, float endpoints,
no docs in production, no PINs or ShapIDs in logs, and no serving on a schema
that is behind the code."""

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import make_engine
from app.log_redaction import RedactShapIds
from app.main import create_app
from app.schema_check import SchemaNotCurrent, check_schema_is_current
from tests.conftest import assert_ledger_balanced

KEY = "a-demo-key-that-is-long-enough-000000"
KEYED = {"X-Demo-Key": KEY}


@pytest.fixture
def keyed_api(clean_db: Engine, test_settings: Settings) -> Iterator[TestClient]:
    settings = test_settings.model_copy(update={"demo_api_key": KEY})
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def production_api(clean_db: Engine, test_settings: Settings) -> Iterator[TestClient]:
    settings = test_settings.model_copy(
        update={"environment": "production", "dev_schema": test_settings.test_schema, "demo_api_key": KEY}
    )
    with TestClient(create_app(settings)) as client:
        yield client


# --- /demo behind the key --------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"X-Demo-Key": "wrong"}, {"X-Demo-Key": KEY[:-1]}])
def test_without_the_key_demo_looks_like_it_does_not_exist(keyed_api: TestClient, headers: dict[str, str]) -> None:
    for response in (
        keyed_api.post("/demo/vouchers", json={"amount_cents": 50000}, headers=headers),
        keyed_api.post("/demo/vouchers", content=b"{not json", headers=headers),  # not a 422
        keyed_api.get("/demo/vouchers", headers=headers),
        keyed_api.get("/demo/float", headers=headers),
        keyed_api.post("/demo/float", json={"amount_cents": 100}, headers=headers),
    ):
        assert (response.status_code, response.json()) == (404, {"detail": "Not Found"})
        assert "X-Demo-Surface" not in response.headers
    assert keyed_api.get("/no-such-path").json() == {"detail": "Not Found"}  # same as a real 404


def test_with_the_key_demo_works(keyed_api: TestClient) -> None:
    response = keyed_api.post("/demo/vouchers", json={"amount_cents": 50000}, headers=KEYED)
    assert response.status_code == 201
    assert response.headers["X-Demo-Surface"] == "true"
    assert keyed_api.get("/demo/vouchers", headers=KEYED).status_code == 200


def test_a_keyed_demo_is_left_out_of_the_openapi_schema(keyed_api: TestClient) -> None:
    paths = keyed_api.get("/openapi.json").json()["paths"]
    assert not any(path.startswith("/demo") for path in paths)
    assert "/v1/deposits" in paths


def test_v1_needs_no_key(keyed_api: TestClient) -> None:
    response = keyed_api.post("/v1/vouchers/lookup", json={"pin": "0000000000000000"})
    assert (response.status_code, response.json()) == (404, {"reason": "voucher_not_found"})


# --- the float endpoints -----------------------------------------------------------------


def test_the_float_can_be_read_and_topped_up(keyed_api: TestClient, clean_db: Engine) -> None:
    assert keyed_api.get("/demo/float", headers=KEYED).json() == {
        "settlement_cents": 0,
        "settlement_display": "R0.00",
        "available_cents": 0,
        "available_display": "R0.00",
    }
    topped = keyed_api.post("/demo/float", json={"amount_cents": 1_000_000}, headers=KEYED)
    assert topped.status_code == 200
    assert topped.headers["X-Demo-Surface"] == "true"
    assert (topped.json()["settlement_display"], topped.json()["available_cents"]) == ("R10 000.00", 1_000_000)
    with Session(clean_db) as session:
        assert_ledger_balanced(session)


@pytest.mark.parametrize("amount", [0, -100, 1000.0, "1000", 100_000_001])
def test_a_top_up_must_be_a_positive_whole_number_of_cents_within_the_cap(
    keyed_api: TestClient, amount: object
) -> None:
    response = keyed_api.post("/demo/float", json={"amount_cents": amount}, headers=KEYED)
    assert response.status_code == 422


# --- production -----------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_production_serves_no_docs(production_api: TestClient, path: str) -> None:
    assert production_api.get(path).status_code == 404


def test_production_serves_health_v1_and_a_keyed_demo(production_api: TestClient) -> None:
    assert production_api.get("/health").json() == {"status": "ok", "database": "ok"}
    assert production_api.post("/v1/vouchers/lookup", json={"pin": "0000000000000000"}).status_code == 404
    assert production_api.get("/demo/float").status_code == 404
    assert production_api.get("/demo/float", headers=KEYED).status_code == 200


# --- logs -------------------------------------------------------------------------------------


def test_sql_errors_do_not_carry_their_parameters(engine: Engine) -> None:
    pin = "1234567890123456"
    with Session(engine) as session, pytest.raises(DBAPIError) as excinfo:
        session.execute(text("SELECT * FROM no_such_table WHERE pin = :pin"), {"pin": pin})
    assert pin not in str(excinfo.value)
    assert engine.hide_parameters


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1/shapid/%2B27821234567", "/v1/shapid/***"),
        ("/v1/shapid/%2B27821234567%40fnb", "/v1/shapid/***"),
        ("/v1/deposits/abc", "/v1/deposits/abc"),
    ],
)
def test_shapids_are_masked_in_the_access_log(path: str, expected: str) -> None:
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", path, "1.1", 200), None,
    )
    assert RedactShapIds().filter(record)
    assert record.getMessage() == f'127.0.0.1:5000 - "GET {expected} HTTP/1.1" 200'


# --- schema version at startup ---------------------------------------------------------------


def test_startup_refuses_a_schema_that_was_never_migrated(engine: Engine, test_settings: Settings) -> None:
    settings = test_settings.model_copy(update={"test_schema": "kd_never_migrated"})
    with pytest.raises(SchemaNotCurrent, match="run `python -m alembic upgrade head`"):
        with TestClient(create_app(settings)):
            pass


def test_startup_accepts_a_current_schema(engine: Engine, test_settings: Settings) -> None:
    current = make_engine(test_settings.database_url, test_settings.test_schema)
    try:
        check_schema_is_current(current)
    finally:
        current.dispose()


def test_an_unreachable_database_at_startup_does_not_stop_the_process() -> None:
    unreachable = make_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:1/none", "public")
    try:
        check_schema_is_current(unreachable)  # logs a warning; /health then reports it
    finally:
        unreachable.dispose()
