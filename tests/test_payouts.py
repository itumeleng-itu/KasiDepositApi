"""The mock payout provider: timeline, failure triggers, idempotency, restarts.

A fake clock drives the timeline, so nothing here sleeps.
"""

import pytest

from app.models import PayoutStatus
from app.payouts import (
    COMPLETED_AFTER_SECONDS,
    FAILURE_TRIGGERS,
    SUBMITTED_AFTER_SECONDS,
    MockPayoutProvider,
    PayoutRef,
    UnknownPayout,
)

DESTINATION = {"kind": "shap_id", "shap_id": "+27821234560"}

# The app's ClearingFailure reasons (src/api/types.ts): anything else renders as "unknown".
APP_CLEARING_FAILURES = {
    "insufficient_float",
    "limit_exceeded",
    "bank_unavailable",
    "bank_processing_error",
    "unknown",
}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def provider(clock: FakeClock) -> MockPayoutProvider:
    return MockPayoutProvider(clock=clock)


def _timeline(provider: MockPayoutProvider, clock: FakeClock, ref: PayoutRef) -> list[str]:
    points = [0.0, SUBMITTED_AFTER_SECONDS - 0.01, SUBMITTED_AFTER_SECONDS, COMPLETED_AFTER_SECONDS - 0.01, COMPLETED_AFTER_SECONDS, 60.0]
    seen = []
    start = clock.now
    for point in points:
        clock.now = start + point
        status = provider.status(ref)
        seen.append(status.status.value + (f":{status.failure_reason}" if status.failure_reason else ""))
    return seen


@pytest.mark.parametrize("amount", [49500, 19500, 99500, 550, 12345])
def test_ordinary_payouts_go_pending_submitted_completed(
    provider: MockPayoutProvider, clock: FakeClock, amount: int
) -> None:
    ref = provider.create(amount, DESTINATION, f"key-{amount}")
    assert _timeline(provider, clock, ref) == [
        "pending", "pending", "submitted", "submitted", "completed", "completed"
    ]


@pytest.mark.parametrize(
    ("amount", "reason"),
    [(40000, "bank_processing_error"), (40100, "limit_exceeded"), (40200, "bank_unavailable")],
)
def test_trigger_amounts_fail_with_their_documented_reason(
    provider: MockPayoutProvider, clock: FakeClock, amount: int, reason: str
) -> None:
    ref = provider.create(amount, DESTINATION, f"key-{amount}")
    assert _timeline(provider, clock, ref) == [
        "pending", "pending", "submitted", "submitted", f"failed:{reason}", f"failed:{reason}"
    ]


@pytest.mark.parametrize("amount", [39900, 40001, 40300])
def test_amounts_next_to_the_triggers_complete(
    provider: MockPayoutProvider, clock: FakeClock, amount: int
) -> None:
    ref = provider.create(amount, DESTINATION, f"key-{amount}")
    clock.advance(COMPLETED_AFTER_SECONDS)
    assert provider.status(ref).status is PayoutStatus.COMPLETED


def test_every_failure_reason_is_one_the_app_knows() -> None:
    assert set(FAILURE_TRIGGERS.values()) <= APP_CLEARING_FAILURES
    assert "invalid_account" not in FAILURE_TRIGGERS.values()


def test_the_same_idempotency_key_returns_the_same_payout(
    provider: MockPayoutProvider, clock: FakeClock
) -> None:
    first = provider.create(49500, DESTINATION, "key-1")
    clock.advance(1)
    assert provider.create(49500, DESTINATION, "key-1") == first
    assert provider.create(49500, DESTINATION, "key-2") != first


def test_status_survives_a_restart(provider: MockPayoutProvider, clock: FakeClock) -> None:
    ref = provider.create(40200, DESTINATION, "key-1")
    restarted = MockPayoutProvider(clock=clock)  # no memory of creating it
    clock.advance(COMPLETED_AFTER_SECONDS)
    status = restarted.status(ref)
    assert (status.status, status.failure_reason) == (PayoutStatus.FAILED, "bank_unavailable")


@pytest.mark.parametrize("ref", ["", "po_1", "mockpo_x_1_abc", "mockpo_100_1"])
def test_an_unknown_reference_raises(provider: MockPayoutProvider, ref: str) -> None:
    with pytest.raises(UnknownPayout):
        provider.status(PayoutRef(ref))


@pytest.mark.parametrize("amount", [0, -100])
def test_create_needs_a_positive_amount(provider: MockPayoutProvider, amount: int) -> None:
    with pytest.raises(ValueError):
        provider.create(amount, DESTINATION, "key-1")


def test_create_needs_an_idempotency_key(provider: MockPayoutProvider) -> None:
    with pytest.raises(ValueError):
        provider.create(49500, DESTINATION, "")
