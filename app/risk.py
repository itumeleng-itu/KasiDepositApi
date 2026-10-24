"""Deposit risk: the seam between KasiDeposit and a fraud-scoring provider.

The abuse this is for is social engineering, not hacking: someone is talked
into buying a voucher and reading the PIN to a stranger, or a stolen PIN is
redeemed into the thief's account. Today the scoring is ours, a handful of
rules over rows we already hold; a real provider replaces `RuleBasedRisk`
and nothing else, the same way as the identity and payout mocks.

Signals, each with a rule id (logged, NEVER sent to the client)
--------------------------------------------------------------
    velocity_1h        too many deposits by this user in the last hour
    velocity_24h       in the last 24 hours, against their own daily average
    novel_destination  a payout method used within minutes of being added,
                       by a user who has been paid somewhere else before
    value_anomaly      far above this user's usual (median) amount
    lag_short          redeemed seconds after it was vended
    lag_long           redeemed days after it was vended
    destination_churn  several different destinations in a short window

Each that fires adds its weight to the score, and the score picks a band.
Every number is in app/config.py (RISK_*).

    allow   the deposit goes ahead as before
    warn    nothing is charged: 409 {"reason": "confirmation_required"}. The
            app shows its interstitial and resends with risk_acknowledged.
    review  refused: 403 {"reason": "deposit_declined"}. Nothing is charged
            and the voucher stays active.

The client is told the band's reason and nothing else: never the rule, the
score or a threshold. An explanation would teach someone exactly how to stay
under it.

Where it runs
-------------
`DepositService` gathers the facts in a short read-only transaction, ends it,
and only then asks for a decision, before the float lock, the token lock or
the voucher's row lock are taken. The decision itself (`assess`) is given
plain values and no session, so a real provider can call out over the
network without holding a transaction open (the 10s idle-in-transaction limit).

Fail open
---------
If gathering or scoring raises, the deposit PROCEEDS and the failure is
logged at ERROR. A scoring bug must never strand someone's cash: a voucher is
cash they have already paid for. See `decide`.

No new personal data
--------------------
Nothing here is stored. Destinations are compared as `fingerprint`s: an HMAC
under a key made fresh in each process, held in memory for one request, so
they cannot be matched against anything outside this process. Decisions are
logged with the band, score, rule ids and the user's internal id only, and
the logger carries the personal-number filter (app/log_redaction.py).
"""

import enum
import hashlib
import hmac
import logging
import os
import secrets
import statistics
import threading
import traceback
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from app.config import Settings
from app.money import Cents

logger = logging.getLogger("app.risk")

# The only two things a client ever learns. Opaque on purpose.
CONFIRMATION_REQUIRED = "confirmation_required"
DEPOSIT_DECLINED = "deposit_declined"

# Rule ids for decisions that were not scored.
RULE_ERROR = "risk_error"
RULE_DEMO_FORCED = "demo_forced"

# Made at import, never stored: fingerprints mean nothing outside this process.
_FINGERPRINT_KEY = secrets.token_bytes(32)


class Band(enum.StrEnum):
    ALLOW = "allow"
    WARN = "warn"
    REVIEW = "review"


@dataclass(frozen=True)
class PastDeposit:
    amount_cents: Cents
    created_at: datetime
    destination: str  # a fingerprint, never the destination itself


@dataclass(frozen=True)
class DepositFacts:
    """Everything a decision may use, as plain values: no session, no rows."""

    user_id: uuid.UUID
    now: datetime
    amount_cents: Cents
    voucher_issued_at: datetime
    destination: str  # fingerprint
    destination_added_at: datetime
    history: tuple[PastDeposit, ...]  # this user's deposits, newest first, failed ones left out


@dataclass(frozen=True)
class RiskDecision:
    band: Band
    score: int
    rules: tuple[str, ...]


ALLOW_ON_ERROR = RiskDecision(Band.ALLOW, 0, (RULE_ERROR,))


def fingerprint(destination: Mapping[str, Any]) -> str:
    """A destination as an opaque value that is only good for "same or not"."""
    if destination.get("kind") == "account":
        material = f"account|{destination.get('bank_id')}|{destination.get('account_last4')}"
    else:
        material = f"shap_id|{destination.get('shap_id')}"
    return hmac.new(_FINGERPRINT_KEY, material.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class RiskThresholds:
    warn_score: int
    review_score: int
    history_days: int
    velocity_hour_max: int
    velocity_day_floor: int
    velocity_day_multiplier: float
    novelty_minutes: int
    value_multiplier: float
    value_min_history: int
    lag_short_seconds: int
    lag_long_hours: int
    churn_window_hours: int
    churn_destinations: int
    weights: Mapping[str, int]

    @classmethod
    def from_settings(cls, s: Settings) -> "RiskThresholds":
        return cls(
            warn_score=s.risk_warn_score,
            review_score=s.risk_review_score,
            history_days=s.risk_history_days,
            velocity_hour_max=s.risk_velocity_hour_max,
            velocity_day_floor=s.risk_velocity_day_floor,
            velocity_day_multiplier=s.risk_velocity_day_multiplier,
            novelty_minutes=s.risk_novelty_minutes,
            value_multiplier=s.risk_value_multiplier,
            value_min_history=s.risk_value_min_history,
            lag_short_seconds=s.risk_lag_short_seconds,
            lag_long_hours=s.risk_lag_long_hours,
            churn_window_hours=s.risk_churn_window_hours,
            churn_destinations=s.risk_churn_destinations,
            weights={
                "velocity_1h": s.risk_weight_velocity_hour,
                "velocity_24h": s.risk_weight_velocity_day,
                "novel_destination": s.risk_weight_novelty,
                "value_anomaly": s.risk_weight_value,
                "lag_short": s.risk_weight_lag_short,
                "lag_long": s.risk_weight_lag_long,
                "destination_churn": s.risk_weight_churn,
            },
        )


class RiskAssessor(Protocol):
    def assess(self, facts: DepositFacts) -> RiskDecision:
        """A decision from facts alone. May be slow or remote: no transaction is open."""
        ...


class RuleBasedRisk:
    """Our own rules over the user's history. Replaceable by a provider."""

    def __init__(self, thresholds: RiskThresholds) -> None:
        self.t = thresholds

    def assess(self, facts: DepositFacts) -> RiskDecision:
        rules = [rule for rule, fired in self.signals(facts).items() if fired]
        score = sum(self.t.weights[rule] for rule in rules)
        if score >= self.t.review_score:
            band = Band.REVIEW
        elif score >= self.t.warn_score:
            band = Band.WARN
        else:
            band = Band.ALLOW
        return RiskDecision(band, score, tuple(rules))

    def signals(self, f: DepositFacts) -> dict[str, bool]:
        return {
            "velocity_1h": self._velocity_hour(f),
            "velocity_24h": self._velocity_day(f),
            "novel_destination": self._novelty(f),
            "value_anomaly": self._value_anomaly(f),
            "lag_short": f.now - f.voucher_issued_at < timedelta(seconds=self.t.lag_short_seconds),
            "lag_long": f.now - f.voucher_issued_at > timedelta(hours=self.t.lag_long_hours),
            "destination_churn": self._churn(f),
        }

    def _since(self, f: DepositFacts, delta: timedelta) -> list[PastDeposit]:
        return [d for d in f.history if d.created_at > f.now - delta]

    def _velocity_hour(self, f: DepositFacts) -> bool:
        return len(self._since(f, timedelta(hours=1))) + 1 > self.t.velocity_hour_max

    def _velocity_day(self, f: DepositFacts) -> bool:
        last_day = len(self._since(f, timedelta(hours=24))) + 1
        earlier = len(self._since(f, timedelta(days=self.t.history_days))) - (last_day - 1)
        daily_average = earlier / (self.t.history_days - 1)
        return last_day > max(self.t.velocity_day_floor, self.t.velocity_day_multiplier * daily_average)

    def _novelty(self, f: DepositFacts) -> bool:
        # A first-time user adding a method and using it at once is the normal
        # first deposit, so this needs history somewhere else to mean anything.
        if not f.history or any(d.destination == f.destination for d in f.history):
            return False
        return f.now - f.destination_added_at <= timedelta(minutes=self.t.novelty_minutes)

    def _value_anomaly(self, f: DepositFacts) -> bool:
        if len(f.history) < self.t.value_min_history:
            return False
        usual = statistics.median(d.amount_cents for d in f.history)
        return f.amount_cents > self.t.value_multiplier * usual

    def _churn(self, f: DepositFacts) -> bool:
        recent = self._since(f, timedelta(hours=self.t.churn_window_hours))
        return len({d.destination for d in recent} | {f.destination}) >= self.t.churn_destinations


class RiskOverride:
    """The /demo switch: force a band for a while, so the behaviour can be
    shown without building up a real pattern. In process memory, like the
    rate limits; it expires by itself so a forgotten switch cannot keep
    refusing real users. Only warn and review can be forced: forcing allow
    would only turn the protection off."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._band: Band | None = None
        self._until: datetime | None = None

    def force(self, band: Band, until: datetime) -> None:
        if band is Band.ALLOW:
            raise ValueError("only warn or review can be forced")
        with self._lock:
            self._band, self._until = band, until

    def clear(self) -> None:
        with self._lock:
            self._band, self._until = None, None

    def current(self, now: datetime) -> tuple[Band, datetime] | None:
        with self._lock:
            if self._band is None or self._until is None:
                return None
            if now >= self._until:
                self._band, self._until = None, None
                return None
            return self._band, self._until


def decide(
    assessor: RiskAssessor,
    override: RiskOverride | None,
    gather: Callable[[], DepositFacts | None],
) -> RiskDecision | None:
    """The decision for one deposit, FAILING OPEN.

    `gather` returns None when there is nothing to assess (a replay, or a
    token the deposit flow will refuse on its own); so does this. Any
    exception from gathering or scoring is logged at ERROR and the deposit is
    ALLOWED: refusing on a bug would strand cash the user has already paid.
    """
    try:
        facts = gather()
        if facts is None:
            return None
        forced = override.current(facts.now) if override is not None else None
        if forced is not None:
            decision = RiskDecision(forced[0], 0, (RULE_DEMO_FORCED,))
        else:
            decision = assessor.assess(facts)
    except Exception as exc:  # noqa: BLE001 - fail open is the point
        # The type and where it was raised, never the message: an exception's
        # text can carry what it was looking up (VoucherNotFound carries the PIN).
        frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
        where = f"{os.path.basename(frame.filename)}:{frame.lineno}" if frame else "?"
        logger.error(
            "risk assessment FAILED (%s at %s); FAILING OPEN: the deposit proceeds unscored",
            type(exc).__name__,
            where,
        )
        return ALLOW_ON_ERROR
    log_decision(facts.user_id, decision)
    return decision


def log_decision(user_id: uuid.UUID, decision: RiskDecision) -> None:
    level = logging.INFO if decision.band is Band.ALLOW else logging.WARNING
    logger.log(
        level,
        "risk decision band=%s score=%d rules=%s user=%s",
        decision.band.value,
        decision.score,
        ",".join(decision.rules) or "-",
        user_id,
    )
