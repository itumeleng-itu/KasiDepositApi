import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db import CONNECT_TIMEOUT_SECONDS
from app.main import create_app
from app.routes import health

UNREACHABLE = {"status": "error", "database": "unreachable"}


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def _client_for(test_settings: Settings, database_url: str) -> Iterator[TestClient]:
    settings = test_settings.model_copy(update={"database_url": database_url})
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def refused_db_client(test_settings: Settings) -> Iterator[TestClient]:
    # Nothing listens on port 1: the connection is refused at once.
    yield from _client_for(test_settings, "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none")


@pytest.fixture
def silent_db_client(test_settings: Settings) -> Iterator[TestClient]:
    # A non-routable address: packets vanish, like a dropped wifi link.
    yield from _client_for(test_settings, "postgresql+psycopg://nobody:nothing@10.255.255.1:5432/none")


def test_health_reports_a_refused_connection(refused_db_client: TestClient) -> None:
    response = refused_db_client.get("/health")
    assert response.status_code == 503
    assert response.json() == UNREACHABLE


def test_health_answers_within_the_connect_timeout_when_the_network_is_gone(
    silent_db_client: TestClient,
) -> None:
    started = time.monotonic()
    response = silent_db_client.get("/health")
    elapsed = time.monotonic() - started
    assert response.status_code == 503
    assert response.json() == UNREACHABLE
    assert elapsed < CONNECT_TIMEOUT_SECONDS + 2


def test_deadline_leaves_room_for_a_cold_check_after_connecting() -> None:
    # A cold check connects (up to the connect timeout) and then queries. A
    # deadline equal to the connect timeout reported a slow but healthy
    # database as unreachable.
    assert health.HEALTH_TIMEOUT_SECONDS > CONNECT_TIMEOUT_SECONDS


def test_health_never_waits_past_its_deadline(
    refused_db_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A connection that opened but then stalls mid-query: the deadline still holds.
    def stalled(_factory: sessionmaker[Session]) -> None:
        time.sleep(3)

    monkeypatch.setattr(health, "ping_database", stalled)
    monkeypatch.setattr(health, "HEALTH_TIMEOUT_SECONDS", 0.5)
    started = time.monotonic()
    response = refused_db_client.get("/health")
    assert response.status_code == 503
    assert response.json() == UNREACHABLE
    assert time.monotonic() - started < 2
