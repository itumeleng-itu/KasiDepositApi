from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_reports_unreachable_database() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://nobody:nothing@127.0.0.1:1/none",
        environment="test",
    )
    with TestClient(create_app(settings)) as unreachable:
        response = unreachable.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "error"}
