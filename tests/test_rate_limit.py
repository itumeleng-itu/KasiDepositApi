"""Rate limits on /v1 (app/rate_limit.py).

None of these need the database. The per-IP limits run before
authentication, so an unauthenticated request is counted and answered 401
without a query; the per-token routes get a signed-in user and a stub
service through dependency overrides. A fake clock drives the windows, so
nothing sleeps.
"""

import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.deposits import DepositView, VoucherLookup
from app.main import create_app
from app.rate_limit import UNKNOWN_CLIENT, RateLimiter
from app.routes import health, v1
from app.shapid import ShapIdNotFound
from app.users import token_hash
from tests.conftest import REGISTRATION

SHAP_ID = "+27821234560"
SHAP_PATH = "/v1/shapid/%2B27821234560"
PIN = "1234567890123456"
TOKEN = "an-access-token-that-the-test-made-up"
REAL_IP = "203.0.113.7"
QUOTA_HEADER = re.compile(r"ratelimit|x-ratelimit|remaining", re.IGNORECASE)
KEY_SHAPE = re.compile(
    r"^(shapid|register|lookup|deposit_status):(ip:([0-9.]+|[0-9a-f:]+/64|unknown)|token:[0-9a-f]{64})$"
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class StubDeposits:
    """Just enough of DepositService for the limited routes."""

    def __init__(self) -> None:
        self.status_reads = 0

    def lookup_voucher(self, _session: Any, _pin: str) -> VoucherLookup:
        return VoucherLookup(voucher_token="vt", value_cents=50000, fee_cents=1250, payout_cents=48750)

    def get_deposit(self, _session: Any, deposit_id: uuid.UUID, _user_id: uuid.UUID) -> DepositView:
        self.status_reads += 1
        return DepositView(id=deposit_id, reference="KD-ABC234", status="submitted", payout_cents=48750,
                           failure_reason=None)


def _app(settings: Settings, clock: FakeClock, **limits: int) -> FastAPI:
    app = create_app(settings.model_copy(update=limits))
    limiter: RateLimiter = app.state.rate_limiter
    limiter.clock = clock
    return app


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def api(test_settings: Settings, clock: FakeClock) -> Iterator[TestClient]:
    # No `with`: the lifespan's schema check would need the database.
    yield TestClient(_app(test_settings, clock))


@pytest.fixture
def signed_in(test_settings: Settings, clock: FakeClock) -> Iterator[TestClient]:
    """A client whose token is accepted without the database, with stub deposits."""
    app = _app(test_settings, clock)
    app.dependency_overrides[v1.current_user] = lambda: uuid.UUID(int=1)
    app.state.deposits = StubDeposits()
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    yield client


def _keys(client: TestClient) -> list[str]:
    limiter: RateLimiter = client.app.state.rate_limiter  # type: ignore[attr-defined]
    return limiter.keys()


def _from(ip: str) -> dict[str, str]:
    return {"X-Forwarded-For": ip}


def _assert_rate_limited(response: Any) -> None:
    assert (response.status_code, response.json()) == (429, {"reason": "rate_limited"})
    assert int(response.headers["Retry-After"]) >= 1
    assert not [h for h in response.headers if QUOTA_HEADER.search(h)]


# --- GET /v1/shapid ---------------------------------------------------------------------------


def test_the_shapid_limit_trips_and_resets(api: TestClient, clock: FakeClock) -> None:
    for _ in range(10):
        response = api.get(SHAP_PATH, headers=_from(REAL_IP))
        assert response.status_code == 401
        assert not [h for h in response.headers if QUOTA_HEADER.search(h)]
    _assert_rate_limited(api.get(SHAP_PATH, headers=_from(REAL_IP)))

    clock.advance(60)
    assert api.get(SHAP_PATH, headers=_from(REAL_IP)).status_code == 401


def test_the_shapid_limit_is_checked_before_the_token(signed_in: TestClient) -> None:
    # Signed in or not, the same per-IP count applies: 429 without reaching the directory.
    for _ in range(10):
        signed_in.get("/v1/shapid/not-a-number", headers=_from(REAL_IP))
    _assert_rate_limited(signed_in.get(SHAP_PATH, headers=_from(REAL_IP)))


def test_a_spoofed_forwarded_for_does_not_buy_a_fresh_limit(api: TestClient) -> None:
    # Render appends the real address; whatever the client wrote sits to its left.
    for i in range(10):
        api.get(SHAP_PATH, headers=_from(f"10.0.0.{i}, {REAL_IP}"))
    _assert_rate_limited(api.get(SHAP_PATH, headers=_from(f"198.51.100.99, {REAL_IP}")))


def test_each_client_address_has_its_own_limit(api: TestClient) -> None:
    for _ in range(10):
        api.get(SHAP_PATH, headers=_from(REAL_IP))
    assert api.get(SHAP_PATH, headers=_from("203.0.113.8")).status_code == 401


def test_an_ipv6_client_is_limited_by_its_slash_64(api: TestClient) -> None:
    for i in range(10):
        api.get(SHAP_PATH, headers=_from(f"2001:db8:1:2::{i:x}"))
    _assert_rate_limited(api.get(SHAP_PATH, headers=_from("2001:db8:1:2:ffff::1")))


@pytest.mark.parametrize(
    "header",
    ["not-an-ip", "", " , , ", "999.1.1.1", "[::1", "x" * 10_000, "éé"],
)
def test_a_malformed_forwarded_for_never_crashes_and_shares_one_bucket(
    api: TestClient, header: str
) -> None:
    response = api.get(SHAP_PATH, headers={"X-Forwarded-For": header.encode("utf-8")})
    assert response.status_code == 401
    assert _keys(api) == [f"shapid:ip:{UNKNOWN_CLIENT}"]


def test_a_missing_forwarded_for_falls_back_to_the_peer(api: TestClient) -> None:
    assert api.get(SHAP_PATH).status_code == 401
    assert _keys(api) == ["shapid:ip:testclient"]  # TestClient's peer name


# --- POST /v1/users ---------------------------------------------------------------------------


def test_the_registration_limit_trips_per_ip(test_settings: Settings, clock: FakeClock) -> None:
    client = TestClient(_app(test_settings, clock, rate_limit_register_requests=3))
    for _ in range(3):
        # An invalid body: the limit is counted first, then validation refuses it, no database.
        assert client.post("/v1/users", json={}, headers=_from(REAL_IP)).status_code == 422
    _assert_rate_limited(client.post("/v1/users", json={}, headers=_from(REAL_IP)))
    assert client.post("/v1/users", json={}, headers=_from("203.0.113.8")).status_code == 422


# --- POST /v1/vouchers/lookup -----------------------------------------------------------------


def test_the_lookup_limit_is_per_token(signed_in: TestClient) -> None:
    for _ in range(10):
        response = signed_in.post("/v1/vouchers/lookup", json={"pin": PIN})
        assert response.status_code == 200
        assert not [h for h in response.headers if QUOTA_HEADER.search(h)]
    _assert_rate_limited(signed_in.post("/v1/vouchers/lookup", json={"pin": PIN}))

    other = {"Authorization": "Bearer a-different-token"}
    assert signed_in.post("/v1/vouchers/lookup", json={"pin": PIN}, headers=other).status_code == 200


# --- GET /v1/deposits/{id} --------------------------------------------------------------------


def _poll_delay_seconds(elapsed: float) -> float:
    """The app's schedule, from src/polling.ts (pinned by src/polling.test.ts)."""
    if elapsed < 30:
        return 1
    if elapsed < 90:
        return 3
    return 5


def test_the_apps_polling_is_never_limited(signed_in: TestClient, clock: FakeClock) -> None:
    deposit = f"/v1/deposits/{uuid.uuid4()}"
    elapsed = 0.0
    while elapsed < 10 * 60:
        assert signed_in.get(deposit).status_code == 200, f"refused at {elapsed}s"
        if elapsed in (10, 20, 25):
            # Back from the background: the app polls straight away, on top of the schedule.
            assert signed_in.get(deposit).status_code == 200
        step = _poll_delay_seconds(elapsed)
        clock.advance(step)
        elapsed += step


def test_the_history_screen_and_the_status_screen_together_stay_under(signed_in: TestClient) -> None:
    # Worst honest case inside one minute: a full minute of 1 s polling, plus opening a few
    # deposits from history (one read each).
    for _ in range(60 + 10):
        assert signed_in.get(f"/v1/deposits/{uuid.uuid4()}").status_code == 200


def test_the_deposit_status_limit_trips_well_above_polling(signed_in: TestClient) -> None:
    deposit = f"/v1/deposits/{uuid.uuid4()}"
    for _ in range(120):
        assert signed_in.get(deposit).status_code == 200
    _assert_rate_limited(signed_in.get(deposit))


# --- /health ----------------------------------------------------------------------------------


def test_health_is_never_rate_limited(
    test_settings: Settings, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Render's health check: every limit at 1, and it still answers every time.
    monkeypatch.setattr(health, "ping_database", lambda _factory: None)
    client = TestClient(
        _app(test_settings, clock, rate_limit_shapid_requests=1, rate_limit_register_requests=1,
             rate_limit_lookup_requests=1, rate_limit_deposit_status_requests=1)
    )
    for _ in range(25):
        assert client.get("/health", headers=_from(REAL_IP)).status_code == 200
    assert _keys(client) == []


# --- what the limiter stores ------------------------------------------------------------------


def test_no_sensitive_value_is_ever_a_key(signed_in: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # The directory itself is not under test: the signed-in ShapID read stops short of it.
    def not_found(*_args: Any, **_kwargs: Any) -> Any:
        raise ShapIdNotFound()

    monkeypatch.setattr(v1, "demo_directory", lambda _session: None)
    monkeypatch.setattr(v1, "resolve", not_found)
    deposit_id = str(uuid.uuid4())
    signed_in.get(SHAP_PATH, headers=_from(REAL_IP))
    # The ID number is in the body; full_names is missing, so it stops at validation.
    signed_in.post("/v1/users", json={"id_number": REGISTRATION["id_number"]}, headers=_from(REAL_IP))
    signed_in.post("/v1/vouchers/lookup", json={"pin": PIN})
    signed_in.get(f"/v1/deposits/{deposit_id}")

    keys = _keys(signed_in)
    assert {k.split(":", 1)[0] for k in keys} == {"shapid", "register", "lookup", "deposit_status"}
    forbidden = [
        PIN, SHAP_ID, SHAP_ID.lstrip("+"), "821234560", "%2B27821234560", REGISTRATION["id_number"],
        TOKEN, token_hash(TOKEN), deposit_id, "vt",
    ]
    for key in keys:
        assert KEY_SHAPE.fullmatch(key), key
        for value in forbidden:
            assert value not in key, (value, key)


def test_the_limiter_is_built_from_settings(test_settings: Settings) -> None:
    limiter: RateLimiter = create_app(
        test_settings.model_copy(update={"rate_limit_shapid_requests": 3, "trusted_proxy_hops": 2})
    ).state.rate_limiter
    assert limiter.limits.shapid.requests == 3
    assert limiter.trusted_proxy_hops == 2
