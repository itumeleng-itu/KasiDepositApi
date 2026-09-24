"""The payout provider: the seam between KasiDeposit and the bank rail.

Modelled on documented South African payout APIs (Stitch, Peach): a payout is
created with an idempotency key and runs asynchronously. `create` returns a
reference immediately, and the outcome is learnt by polling `status`, never
returned inline. A real provider replaces `MockPayoutProvider` and nothing else.

The mock
--------
It advances on a clock, so the app's polling has something real to poll:

    pending    until 1.5 s after creation
    submitted  until 3 s
    completed  (or failed, for the trigger amounts below)

Failure triggers, by payout amount, so any outcome can be shown on stage
without touching config:

    R400.00 exactly   failed, bank_processing_error
    R401.00 exactly   failed, limit_exceeded
    R402.00 exactly   failed, bank_unavailable
    anything else     completed (whole rand and cents alike)

`insufficient_float` is not a provider outcome: we check our own float before
charging the voucher, so a payout the float cannot cover is never instructed
(see app/ledger.py).

The amount and creation time are encoded in the payout reference, so `status`
keeps answering after a restart. Idempotency (the same key returning the same
reference) is remembered in memory, as a real provider would remember it in
its own database; our `payouts` table records every reference we were given,
so we never ask twice for the same deposit.

Failure reasons are exactly the app's `ClearingFailure` strings.
"""

import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from app.models import PayoutStatus
from app.money import Cents

SUBMITTED_AFTER_SECONDS = 1.5
COMPLETED_AFTER_SECONDS = 3.0

FAILURE_TRIGGERS: dict[Cents, str] = {
    40000: "bank_processing_error",
    40100: "limit_exceeded",
    40200: "bank_unavailable",
}

_REF_PREFIX = "mockpo"


@dataclass(frozen=True)
class PayoutRef:
    provider_ref: str


@dataclass(frozen=True)
class ProviderStatus:
    status: PayoutStatus
    failure_reason: str | None = None  # set only when status is FAILED


class UnknownPayout(LookupError):
    """The provider has no payout with this reference."""


class PayoutUnavailable(Exception):
    """The provider could not be reached. Not a payout failure: nothing is
    known to have happened, so the caller leaves the deposit as it is and
    tries again later (see app/deposits.py)."""


class PayoutProvider(Protocol):
    def create(
        self, amount_cents: Cents, destination: Mapping[str, Any], idempotency_key: str
    ) -> PayoutRef: ...

    def status(self, payout_ref: PayoutRef) -> ProviderStatus: ...


class MockPayoutProvider:
    """In-process stand-in for a payout API. See the module docstring."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._refs_by_key: dict[str, PayoutRef] = {}

    def create(
        self, amount_cents: Cents, destination: Mapping[str, Any], idempotency_key: str
    ) -> PayoutRef:
        if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents <= 0:
            raise ValueError("a payout needs a positive number of cents")
        if not idempotency_key:
            raise ValueError("a payout needs an idempotency key")
        existing = self._refs_by_key.get(idempotency_key)
        if existing is not None:
            return existing
        created_ms = int(self._clock() * 1000)
        tag = hashlib.sha256(idempotency_key.encode()).hexdigest()[:10]
        ref = PayoutRef(f"{_REF_PREFIX}_{amount_cents}_{created_ms}_{tag}")
        self._refs_by_key[idempotency_key] = ref
        return ref

    def status(self, payout_ref: PayoutRef) -> ProviderStatus:
        amount_cents, created_ms = _decode(payout_ref)
        elapsed = self._clock() - created_ms / 1000
        if elapsed < SUBMITTED_AFTER_SECONDS:
            return ProviderStatus(PayoutStatus.PENDING)
        if elapsed < COMPLETED_AFTER_SECONDS:
            return ProviderStatus(PayoutStatus.SUBMITTED)
        reason = FAILURE_TRIGGERS.get(amount_cents)
        if reason is not None:
            return ProviderStatus(PayoutStatus.FAILED, reason)
        return ProviderStatus(PayoutStatus.COMPLETED)


def _decode(payout_ref: PayoutRef) -> tuple[Cents, int]:
    parts = payout_ref.provider_ref.split("_")
    if len(parts) != 4 or parts[0] != _REF_PREFIX or not parts[1].isdigit() or not parts[2].isdigit():
        raise UnknownPayout(payout_ref.provider_ref)
    return int(parts[1]), int(parts[2])
