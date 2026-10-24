"""Deposit risk (app/risk.py): the signals, the bands, failing open, the logs,
what the client is told, and the /demo switch.

None of these need the database. The signals are pure functions of
`DepositFacts`; the route tests use the real app and the real
`DepositService._screen`, with only the read that gathers the facts (and the
signed-in user and payout method) stood in for.
"""

import logging
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.deposits import DepositService, Refused
from app.log_redaction import install as install_log_redaction
from app.main import create_app
from app.payout_methods import DepositDestination
from app.risk import (
    ALLOW_ON_ERROR,
    Band,
    DepositFacts,
    PastDeposit,
    RiskDecision,
    RiskOverride,
    RiskThresholds,
    RuleBasedRisk,
    decide,
    fingerprint,
)
from app.routes import v1
from app.switch import VoucherNotFound

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
USER = uuid.UUID("8f1c2d3e-1234-4abc-9def-123456789012")
PIN = "1234567890123456"
ID_NUMBER = "8001015009087"
SHAP_ID = "+27821234560"
ACCOUNT_NUMBER = "62123456789"
HOME = fingerprint({"kind": "shap_id", "shap_id": SHAP_ID})
OTHER = fingerprint({"kind": "account", "bank_id": "fnb", "account_last4": "6789"})
RULE_IDS = (
    "velocity_1h", "velocity_24h", "novel_destination", "value_anomaly",
    "lag_short", "lag_long", "destination_churn", "demo_forced", "risk_error",
)
DEMO_KEY = "a-demo-key-that-is-long-enough-000000"


def _past(ago: timedelta, amount: int = 20000, destination: str = HOME) -> PastDeposit:
    return PastDeposit(amount_cents=amount, created_at=NOW - ago, destination=destination)


def _facts(**overrides: Any) -> DepositFacts:
    """An unremarkable deposit: a usual amount, to the usual place, vended
    ten minutes ago, by a user with a quiet fortnight of history."""
    values: dict[str, Any] = {
        "user_id": USER,
        "now": NOW,
        "amount_cents": 20000,
        "voucher_issued_at": NOW - timedelta(minutes=10),
        "destination": HOME,
        "destination_added_at": NOW - timedelta(days=60),
        "history": tuple(_past(timedelta(days=d)) for d in (2, 5, 9, 14)),
    }
    values.update(overrides)
    return DepositFacts(**values)


@pytest.fixture
def risk(test_settings: Settings) -> RuleBasedRisk:
    return RuleBasedRisk(RiskThresholds.from_settings(test_settings))


def _fired(risk: RuleBasedRisk, facts: DepositFacts) -> set[str]:
    return {rule for rule, fired in risk.signals(facts).items() if fired}


# --- the signals: one each ------------------------------------------------------------------


def test_an_ordinary_deposit_fires_nothing(risk: RuleBasedRisk) -> None:
    assert _fired(risk, _facts()) == set()
    assert risk.assess(_facts()) == RiskDecision(Band.ALLOW, 0, ())


def test_velocity_in_the_last_hour(risk: RuleBasedRisk) -> None:
    history = tuple(_past(timedelta(minutes=m)) for m in (5, 20, 40))  # this one makes four
    assert "velocity_1h" in _fired(risk, _facts(history=history))
    assert "velocity_1h" not in _fired(risk, _facts(history=history[:2]))


def test_velocity_over_a_day_against_the_users_own_pattern(risk: RuleBasedRisk) -> None:
    today = tuple(_past(timedelta(hours=h)) for h in (2, 5, 8, 11, 14))  # this one makes six
    assert "velocity_24h" in _fired(risk, _facts(history=today))
    # A user who has always done about this many a day is not unusual at six.
    habitual = today + tuple(_past(timedelta(days=d, hours=h)) for d in range(1, 29) for h in (1, 3, 5))
    assert "velocity_24h" not in _fired(risk, _facts(history=habitual))


def test_a_payout_method_used_minutes_after_it_was_added(risk: RuleBasedRisk) -> None:
    fresh = {"destination": OTHER, "destination_added_at": NOW - timedelta(minutes=3)}
    assert "novel_destination" in _fired(risk, _facts(**fresh))
    # Added long ago, or the user's very first deposit (the normal sign-up path): not novel.
    assert "novel_destination" not in _fired(risk, _facts(destination=OTHER))
    assert "novel_destination" not in _fired(risk, _facts(**fresh, history=()))


def test_a_value_far_outside_the_users_usual(risk: RuleBasedRisk) -> None:
    assert "value_anomaly" in _fired(risk, _facts(amount_cents=100_000))  # 5x the R200 median
    assert "value_anomaly" not in _fired(risk, _facts(amount_cents=60_000))
    # Too little history to say what is usual.
    assert "value_anomaly" not in _fired(risk, _facts(amount_cents=100_000, history=_facts().history[:2]))


def test_a_redemption_seconds_after_the_vend(risk: RuleBasedRisk) -> None:
    assert "lag_short" in _fired(risk, _facts(voucher_issued_at=NOW - timedelta(seconds=5)))
    assert "lag_short" not in _fired(risk, _facts(voucher_issued_at=NOW - timedelta(minutes=2)))


def test_a_redemption_days_after_the_vend(risk: RuleBasedRisk) -> None:
    assert "lag_long" in _fired(risk, _facts(voucher_issued_at=NOW - timedelta(days=5)))
    assert "lag_long" not in _fired(risk, _facts(voucher_issued_at=NOW - timedelta(days=2)))


def test_several_destinations_in_a_short_window(risk: RuleBasedRisk) -> None:
    third = fingerprint({"kind": "shap_id", "shap_id": "+27829999999"})
    # Home five hours ago, another account two hours ago, and now a third: three in a day.
    churned = (_past(timedelta(hours=2), destination=OTHER), _past(timedelta(hours=5))) + _facts().history
    assert "destination_churn" in _fired(risk, _facts(destination=third, history=churned))
    # Two in a day, or three spread over a fortnight: not churn.
    assert "destination_churn" not in _fired(risk, _facts(destination=OTHER, history=churned[1:]))
    spread = (_past(timedelta(days=3), destination=OTHER),) + _facts().history
    assert "destination_churn" not in _fired(risk, _facts(destination=third, history=spread))


def test_the_score_picks_the_band(risk: RuleBasedRisk) -> None:
    warn = risk.assess(_facts(voucher_issued_at=NOW - timedelta(days=5)))
    assert (warn.band, warn.score, warn.rules) == (Band.WARN, 30, ("lag_long",))
    review = risk.assess(_facts(voucher_issued_at=NOW - timedelta(days=5), amount_cents=100_000))
    assert review.band is Band.REVIEW
    assert review.score == 60  # 30 + 30
    assert set(review.rules) == {"lag_long", "value_anomaly"}


def test_the_thresholds_come_from_settings(test_settings: Settings) -> None:
    strict = RuleBasedRisk(RiskThresholds.from_settings(
        test_settings.model_copy(update={"risk_lag_long_hours": 1, "risk_weight_lag_long": 80})
    ))
    decision = strict.assess(_facts(voucher_issued_at=NOW - timedelta(hours=2)))
    assert (decision.band, decision.rules) == (Band.REVIEW, ("lag_long",))


def test_fingerprints_compare_destinations_without_holding_them() -> None:
    assert fingerprint({"kind": "shap_id", "shap_id": SHAP_ID}) == HOME
    assert SHAP_ID.lstrip("+") not in HOME and "821234560" not in HOME
    assert re.fullmatch(r"[0-9a-f]{64}", HOME)


# --- fail open -------------------------------------------------------------------------------


class Exploding:
    def assess(self, facts: DepositFacts) -> RiskDecision:
        raise ZeroDivisionError("a scoring bug")


def test_a_scoring_error_fails_open_and_is_logged_loudly(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.risk")
    assert decide(Exploding(), None, _facts) == ALLOW_ON_ERROR
    [record] = [r for r in caplog.records if r.name == "app.risk"]
    assert record.levelno == logging.ERROR
    assert "FAILING OPEN" in record.getMessage() and "ZeroDivisionError" in record.getMessage()


def test_an_error_gathering_the_facts_fails_open_too(caplog: pytest.LogCaptureFixture) -> None:
    def broken() -> DepositFacts:
        raise RuntimeError("the database went away")

    caplog.set_level(logging.ERROR, logger="app.risk")
    assert decide(Exploding(), None, broken) == ALLOW_ON_ERROR
    assert "FAILING OPEN" in caplog.text


def test_the_deposit_proceeds_when_risk_throws(test_settings: Settings) -> None:
    """At the service: a broken assessor must not refuse; the charge goes ahead."""
    service = DepositService(
        switch=MagicMock(), provider=MagicMock(), fee_basis_points=250, min_voucher_cents=1000,
        token_ttl=timedelta(minutes=10), risk=Exploding(),
    )
    service._risk_facts = lambda *args: _facts()  # type: ignore[method-assign]
    session = MagicMock()
    service._screen(session, "vt", {"kind": "shap_id", "shap_id": SHAP_ID}, None, str(uuid.uuid4()), USER, False)
    session.rollback.assert_called()  # the facts' read transaction was ended before deciding


# --- the logs ---------------------------------------------------------------------------------


SENSITIVE = (PIN, ID_NUMBER, SHAP_ID, SHAP_ID.lstrip("+"), "0821234560", ACCOUNT_NUMBER)


def test_decisions_log_score_and_rules_and_no_personal_data(
    risk: RuleBasedRisk, caplog: pytest.LogCaptureFixture
) -> None:
    install_log_redaction()
    caplog.set_level(logging.INFO, logger="app.risk")
    decide(risk, None, lambda: _facts(voucher_issued_at=NOW - timedelta(days=5), amount_cents=100_000))
    [record] = [r for r in caplog.records if r.name == "app.risk"]
    message = record.getMessage()
    assert "band=review" in message and "score=60" in message
    assert "lag_long" in message and "value_anomaly" in message
    assert str(USER) in message  # the internal id survives the filter: it is how a decision is traced
    for value in SENSITIVE:
        assert value not in caplog.text


def test_an_exception_carrying_a_pin_never_reaches_the_log(caplog: pytest.LogCaptureFixture) -> None:
    def lookup_fails() -> DepositFacts:
        raise VoucherNotFound(PIN)  # the switch's own exception: its message is the PIN

    caplog.set_level(logging.INFO, logger="app.risk")
    decide(Exploding(), None, lookup_fails)
    assert "VoucherNotFound" in caplog.text
    assert PIN not in caplog.text


def test_the_risk_logger_masks_personal_numbers_whatever_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    install_log_redaction()
    caplog.set_level(logging.INFO, logger="app.risk")
    logging.getLogger("app.risk").warning(
        "a careless line: pin %s id %s shap %s account %s user %s", PIN, ID_NUMBER, SHAP_ID, ACCOUNT_NUMBER, USER
    )
    for value in SENSITIVE:
        assert value not in caplog.text
    assert str(USER) in caplog.text


# --- through the API: what the client is told -------------------------------------------------


def _app(settings: Settings, facts: DepositFacts | None) -> FastAPI:
    app = create_app(settings.model_copy(update={"demo_api_key": DEMO_KEY, "risk_enabled": True}))
    app.dependency_overrides[v1.current_user] = lambda: USER
    methods = MagicMock()
    methods.destination_for_deposit.return_value = DepositDestination(
        {"kind": "shap_id", "shap_id": SHAP_ID, "shap_name": "M. Mothiba", "bank_id": "capitec"},
        NOW - timedelta(days=60),
    )
    app.state.payout_methods = methods
    deposits: DepositService = app.state.deposits
    deposits._risk_facts = lambda *args: facts  # type: ignore[method-assign]
    app.state.charges = []

    def charge(*_args: Any, **_kwargs: Any) -> Any:
        # Past risk: the charge itself needs the database, so stop here and say so.
        app.state.charges.append("charge")
        raise Refused("voucher_not_found")

    deposits._charge_and_record = charge  # type: ignore[method-assign]
    return app


@pytest.fixture
def risky(test_settings: Settings) -> Iterator[TestClient]:
    # Three rules at once: as clear a review as there is.
    facts = _facts(
        voucher_issued_at=NOW - timedelta(days=5),
        amount_cents=100_000,
        history=tuple(_past(timedelta(minutes=m)) for m in (5, 20, 40)) + _facts().history,
    )
    yield TestClient(_app(test_settings, facts))


def _deposit(client: TestClient, **extra: Any) -> Any:
    return client.post(
        "/v1/deposits",
        json={"voucher_token": "vt", "payout_method_id": str(uuid.uuid4()), **extra},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )


def _says_nothing_about_why(response: Any) -> None:
    exposed = (response.text + " " + " ".join(f"{k}: {v}" for k, v in response.headers.items())).lower()
    for word in (*RULE_IDS, "score", "threshold", "band", "velocity", "lag", "novel", "churn", "anomaly"):
        assert word not in exposed, word


def test_a_review_is_one_opaque_reason(risky: TestClient) -> None:
    response = _deposit(risky)
    assert (response.status_code, response.json()) == (403, {"reason": "deposit_declined"})
    _says_nothing_about_why(response)


def test_acknowledging_never_overrides_a_review(risky: TestClient) -> None:
    response = _deposit(risky, risk_acknowledged=True)
    assert (response.status_code, response.json()) == (403, {"reason": "deposit_declined"})


def test_a_warn_asks_for_confirmation_and_says_nothing_else(test_settings: Settings) -> None:
    client = TestClient(_app(test_settings, _facts(voucher_issued_at=NOW - timedelta(days=5))))
    response = _deposit(client)
    assert (response.status_code, response.json()) == (409, {"reason": "confirmation_required"})
    _says_nothing_about_why(response)


def test_a_warn_that_is_acknowledged_goes_on_to_the_charge(test_settings: Settings) -> None:
    app = _app(test_settings, _facts(voucher_issued_at=NOW - timedelta(days=5)))
    client = TestClient(app)
    assert _deposit(client).status_code == 409
    assert app.state.charges == []  # the warn charged nothing
    _deposit(client, risk_acknowledged=True)
    assert app.state.charges == ["charge"]


def test_refusals_charge_nothing_and_allow_reaches_the_charge(test_settings: Settings) -> None:
    app = _app(test_settings, _facts())
    client = TestClient(app)
    assert _deposit(client).status_code == 404  # the stub charge's refusal: risk let it through
    assert app.state.charges == ["charge"]


# --- /demo/risk -------------------------------------------------------------------------------


KEYED = {"X-Demo-Key": DEMO_KEY}


def test_the_demo_switch_forces_a_band_and_clears(test_settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    client = TestClient(_app(test_settings, _facts()))
    assert _deposit(client).status_code == 404  # allowed, so it reached the (stub) charge

    forced = client.post("/demo/risk", json={"band": "review", "minutes": 5}, headers=KEYED)
    assert forced.status_code == 200
    assert forced.headers["X-Demo-Surface"] == "true"
    assert forced.json()["band"] == "review"
    caplog.set_level(logging.INFO, logger="app.risk")
    response = _deposit(client)
    assert (response.status_code, response.json()) == (403, {"reason": "deposit_declined"})
    assert "rules=demo_forced" in caplog.text  # logged as forced, so it is never mistaken for real

    assert client.get("/demo/risk", headers=KEYED).json()["band"] == "review"
    cleared = client.delete("/demo/risk", headers=KEYED)
    assert cleared.status_code == 204 and cleared.headers["X-Demo-Surface"] == "true"
    assert client.get("/demo/risk", headers=KEYED).json() == {"band": None, "expires_at": None}
    assert _deposit(client).status_code == 404


def test_the_demo_switch_forces_a_warn(test_settings: Settings) -> None:
    client = TestClient(_app(test_settings, _facts()))
    client.post("/demo/risk", json={"band": "warn"}, headers=KEYED)
    response = _deposit(client)
    assert (response.status_code, response.json()) == (409, {"reason": "confirmation_required"})


@pytest.mark.parametrize("headers", [{}, {"X-Demo-Key": "wrong"}])
def test_the_demo_switch_is_behind_the_demo_key(test_settings: Settings, headers: dict[str, str]) -> None:
    client = TestClient(_app(test_settings, _facts()))
    for response in (
        client.post("/demo/risk", json={"band": "review"}, headers=headers),
        client.get("/demo/risk", headers=headers),
        client.delete("/demo/risk", headers=headers),
    ):
        assert (response.status_code, response.json()) == (404, {"detail": "Not Found"})
        assert "X-Demo-Surface" not in response.headers
    assert client.app.state.risk_override.current(datetime.now(UTC)) is None  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "body", [{"band": "allow"}, {"band": "review", "minutes": 61}, {"band": "review", "minutes": 0}, {}]
)
def test_the_demo_switch_refuses_allow_and_long_or_odd_requests(test_settings: Settings, body: dict[str, Any]) -> None:
    client = TestClient(_app(test_settings, _facts()))
    response = client.post("/demo/risk", json=body, headers=KEYED)
    assert response.status_code == 422
    assert response.headers["X-Demo-Surface"] == "true"


def test_a_forced_band_expires_by_itself() -> None:
    override = RiskOverride()
    override.force(Band.REVIEW, NOW + timedelta(minutes=5))
    assert override.current(NOW) == (Band.REVIEW, NOW + timedelta(minutes=5))
    assert override.current(NOW + timedelta(minutes=5)) is None
    with pytest.raises(ValueError):
        override.force(Band.ALLOW, NOW + timedelta(minutes=5))


# --- settings --------------------------------------------------------------------------------


def test_risk_can_be_switched_off_without_a_deploy(test_settings: Settings) -> None:
    off = create_app(test_settings.model_copy(update={"risk_enabled": False}))
    on = create_app(test_settings.model_copy(update={"risk_enabled": True}))
    assert off.state.deposits.risk is None
    assert isinstance(on.state.deposits.risk, RuleBasedRisk)


def test_the_warn_score_must_be_below_the_review_score(test_settings: Settings) -> None:
    with pytest.raises(ValueError, match="RISK_WARN_SCORE"):
        Settings(**{**test_settings.model_dump(), "risk_warn_score": 60, "risk_review_score": 60})
