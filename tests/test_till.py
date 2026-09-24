"""The demo till page (GET /till) and the requests it makes."""

import json
import re
import shutil
import subprocess
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.config import Settings
from app.main import create_app
from app.routes.till import TILL_PAGE

KEY = "a-till-code-that-is-long-enough-00000000"


@pytest.fixture
def till_api(clean_db: Engine, test_settings: Settings) -> Iterator[TestClient]:
    settings = test_settings.model_copy(update={"demo_api_key": KEY})
    with TestClient(create_app(settings)) as client:
        yield client


def test_the_till_page_is_served_without_the_key(till_api: TestClient) -> None:
    # A browser opening a URL cannot send a header, so the page itself is open.
    response = till_api.get("/till")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    for expected in ("KasiDeposit Till", "Sell a voucher", "Manager", "/demo/vouchers", "/demo/float", "X-Demo-Key"):
        assert expected in page


def test_the_page_holds_no_secret(till_api: TestClient) -> None:
    assert KEY not in till_api.get("/till").text


def test_the_page_is_not_cached_or_framed(till_api: TestClient) -> None:
    headers = till_api.get("/till").headers
    assert headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"


def test_the_till_is_not_in_the_api_schema(till_api: TestClient) -> None:
    assert "/till" not in till_api.get("/openapi.json").json()["paths"]


def test_no_till_without_the_demo_routes(test_settings: Settings, clean_db: Engine) -> None:
    settings = test_settings.model_copy(update={"enable_demo_routes": False})
    with TestClient(create_app(settings)) as client:
        assert client.get("/till").status_code == 404


def test_what_the_page_does_needs_the_code(till_api: TestClient) -> None:
    """The page's requests, exactly as it makes them: with the code they work,
    without it they are refused as if /demo did not exist."""
    sale = {"amount_cents": 50000}
    assert till_api.post("/demo/vouchers", json=sale).status_code == 404
    sold = till_api.post("/demo/vouchers", json=sale, headers={"X-Demo-Key": KEY})
    assert sold.status_code == 201
    body = sold.json()
    assert {"pin", "serial", "amount_display", "issued_at"} <= set(body)  # what the slip shows

    assert till_api.get("/demo/float").status_code == 404
    topped = till_api.post("/demo/float", json={"amount_cents": 1_000_000}, headers={"X-Demo-Key": KEY})
    assert topped.status_code == 200
    assert {"available_display", "settlement_display"} <= set(topped.json())


# --- the page's money handling, run in Node ------------------------------------------

_FUNCTIONS = ("randToCents", "formatRand", "groupPin")


def _page_functions() -> str:
    script = re.search(r"<script>(.*?)</script>", TILL_PAGE.read_text(encoding="utf-8"), re.S)
    assert script is not None
    source = script.group(1)
    parts = []
    for name in _FUNCTIONS:
        match = re.search(rf"function {name}\(.*?\n}}\n", source, re.S)
        assert match is not None, name
        parts.append(match.group(0))
    return "\n".join(parts)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_the_page_converts_rand_to_whole_cents() -> None:
    cases_in = ["50", "150.5", "150.50", "R1 000", "1000,25", "0.01", "abc", "1.234", "", "-5"]
    program = _page_functions() + (
        f"\nconst inputs = {json.dumps(cases_in)};"
        "\nconsole.log(JSON.stringify({"
        " cents: inputs.map(randToCents),"
        " rand: [0, 5, 50000, 123456, 100000000, -150].map(formatRand),"
        " pin: groupPin('0754762219816030') }));"
    )
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=30, check=True)
    out = json.loads(result.stdout)
    assert out["cents"] == [5000, 15050, 15050, 100000, 100025, 1, None, None, None, None]
    assert out["rand"] == ["R0.00", "R0.05", "R500.00", "R1 234.56", "R1 000 000.00", "-R1.50"]
    assert out["pin"] == "0754 7622 1981 6030"
