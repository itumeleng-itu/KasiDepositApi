from collections.abc import Mapping
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.config import Settings
from app.main import create_app

URL = "/demo/vouchers"


def _assert_demo_header(headers: Mapping[str, str]) -> None:
    assert headers.get("X-Demo-Surface") == "true"


def test_vend_returns_201_with_documented_shape(client: TestClient) -> None:
    response = client.post(URL, json={"amount_cents": 50000})
    assert response.status_code == 201
    _assert_demo_header(response.headers)

    body = response.json()
    assert set(body) == {
        "pin", "serial", "amount_cents", "amount_display", "issued_at", "qr_payload", "qr_svg"
    }
    assert isinstance(body["pin"], str) and len(body["pin"]) == 16 and body["pin"].isdigit()
    assert isinstance(body["serial"], str) and len(body["serial"]) == 20
    assert body["amount_cents"] == 50000
    assert body["amount_display"] == "R500.00"
    assert datetime.fromisoformat(body["issued_at"]).tzinfo is not None


@pytest.mark.parametrize("amount", [1000, 1001, 499999, 500000])
def test_amounts_at_and_inside_the_limits_are_accepted(client: TestClient, amount: int) -> None:
    response = client.post(URL, json={"amount_cents": amount})
    assert response.status_code == 201
    assert response.json()["amount_cents"] == amount


@pytest.mark.parametrize("amount", [999, 500001, 0, -1000])
def test_amounts_outside_the_limits_are_422(client: TestClient, amount: int) -> None:
    response = client.post(URL, json={"amount_cents": amount})
    assert response.status_code == 422
    _assert_demo_header(response.headers)
    assert response.json()["detail"][0]["loc"] == ["body", "amount_cents"]


@pytest.mark.parametrize("amount", [50000.0, 50000.5, "50000", True, None])
def test_non_integer_amounts_are_422(client: TestClient, amount: object) -> None:
    response = client.post(URL, json={"amount_cents": amount})
    assert response.status_code == 422
    _assert_demo_header(response.headers)


def test_float_amount_is_rejected_not_rounded(client: TestClient, clean_db: Engine) -> None:
    client.post(URL, json={"amount_cents": 50000.0})
    with clean_db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM vouchers")).scalar_one() == 0


def test_unknown_fields_are_422(client: TestClient) -> None:
    response = client.post(URL, json={"amount_cents": 50000, "user_id": 7})
    assert response.status_code == 422


def test_list_shows_recent_vouchers_with_masked_pins(client: TestClient) -> None:
    first = client.post(URL, json={"amount_cents": 10000}).json()
    second = client.post(URL, json={"amount_cents": 20000}).json()

    response = client.get(URL)
    assert response.status_code == 200
    _assert_demo_header(response.headers)
    items = response.json()

    assert [i["serial"] for i in items] == [second["serial"], first["serial"]]
    assert items[0]["pin_masked"] == "*" * 12 + second["pin"][-4:]
    assert items[0]["status"] == "active"
    assert items[0]["redeemed_at"] is None
    assert items[0]["amount_display"] == "R200.00"
    assert first["pin"] not in response.text
    assert second["pin"] not in response.text


def test_list_is_capped_at_50(client: TestClient, clean_db: Engine) -> None:
    with clean_db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO vouchers (pin, amount_cents, status, serial, issued_at)"
                " SELECT lpad(n::text, 16, '0'), 10000, 'active',"
                "        '20260924' || lpad(n::text, 12, '0'),"
                "        now() - make_interval(secs => n)"
                " FROM generate_series(1, 51) AS n"
            )
        )
    items = client.get(URL).json()
    assert len(items) == 50
    # Newest first: n = 1 is the most recent, n = 51 the oldest and dropped.
    assert items[0]["serial"].endswith("000000000001")
    assert all(not i["serial"].endswith("000000000051") for i in items)


def test_health_is_not_marked_as_demo(client: TestClient) -> None:
    assert "X-Demo-Surface" not in client.get("/health").headers


def test_demo_routes_are_tagged_in_openapi(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert paths[URL]["post"]["tags"] == ["demo"]
    assert paths[URL]["get"]["tags"] == ["demo"]


def test_demo_routes_absent_when_disabled(test_settings: Settings) -> None:
    settings = test_settings.model_copy(update={"enable_demo_routes": False})
    with TestClient(create_app(settings)) as disabled:
        post = disabled.post(URL, json={"amount_cents": 50000})
        assert post.status_code == 404
        assert "X-Demo-Surface" not in post.headers
        assert disabled.get(URL).status_code == 404
        assert not any(p.startswith("/demo") for p in disabled.get("/openapi.json").json()["paths"])
