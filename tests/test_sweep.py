"""Finishing deposits nobody is polling for (app/deposits.py `sweep`).

The failure these guard against: a deposit's voucher is redeemed in the
same transaction that records it, so from `charged` onwards the
customer's cash is spent. The only thing that used to move a deposit on
was the depositor reading its status, and `get_deposit` serves a deposit
only to the user who made it — so a phone that went away left its
deposit charged for good, with the money neither arrived nor returned.
"""

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db import make_session_factory
from app.deposits import DepositService, Refused
from app.ledger import fund_float
from app.models import Deposit, DepositStatus, Voucher, VoucherStatus
from app.payouts import MockPayoutProvider, PayoutRef, PayoutUnavailable, ProviderStatus
from app.switch import VoucherSwitch
from tests.conftest import assert_ledger_balanced, make_user

S = DepositStatus
# Comfortably past the sweeper's default staleness threshold.
STALE = 120
DESTINATION = {"kind": "shap_id", "shap_id": "+27821234560"}


class Clock:
    """One clock for the whole test, moved by hand.

    It drives both the payout provider and the deposit service, so
    advancing it does two things at once: a payout becomes completable,
    and every deposit becomes that much staler. That is how time passing
    is simulated here — `deposits.updated_at` is maintained by a database
    trigger, so an UPDATE that tried to backdate a row would fire the
    trigger and set it straight back to now.
    """

    def __init__(self) -> None:
        # Starts at the real clock, deliberately: `deposits.updated_at` is
        # written by the database's own now(), so a fake clock set to some
        # arbitrary epoch would put every cutoff decades before any row and
        # nothing would ever look stale.
        self.t = time.time()

    def __call__(self) -> float:
        return self.t

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.t, UTC)

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FlakyProvider:
    """The mock provider with a switch on it. `online = False` is a provider
    that cannot be reached — which leaves a deposit exactly where it was,
    and is the state that used to strand it."""

    def __init__(self, clock: Clock) -> None:
        self.inner = MockPayoutProvider(clock=clock)
        self.online = True

    def create(self, amount_cents: int, destination: dict, idempotency_key: str) -> PayoutRef:
        if not self.online:
            raise PayoutUnavailable("provider down")
        return self.inner.create(amount_cents, destination, idempotency_key)

    def status(self, payout_ref: PayoutRef) -> ProviderStatus:
        if not self.online:
            raise PayoutUnavailable("provider down")
        return self.inner.status(payout_ref)


@pytest.fixture
def setup(clean_db: Engine):
    """A service with a controllable clock and provider, plus a funded float."""
    clock = Clock()
    provider = FlakyProvider(clock)
    switch = VoucherSwitch(1000, 500000)
    service = DepositService(
        switch=switch,
        provider=provider,
        fee_basis_points=250,
        min_voucher_cents=1000,
        token_ttl=timedelta(minutes=10),
        now=clock.now,
    )
    factory = make_session_factory(clean_db)
    with factory() as session:
        fund_float(session, 1_000_000)
        session.commit()
        user_id = make_user(session)
    return service, switch, provider, clock, factory, user_id


def _charge_a_deposit(setup, amount_cents: int = 50000) -> uuid.UUID:
    """Create a deposit while the provider is unreachable, so it commits as
    `charged` with no payout instructed — the stranded state."""
    service, switch, provider, _clock, factory, user_id = setup
    provider.online = False
    with factory() as session:
        pin = switch.vend(session, amount_cents).pin
        session.commit()
        token = service.lookup_voucher(session, pin).voucher_token
        view, created = service.create_deposit(session, token, DESTINATION, str(uuid.uuid4()), user_id)
        assert created
    with factory() as session:
        deposit = session.get(Deposit, view.id)
        assert deposit is not None
        assert deposit.status is S.CHARGED  # the money is taken, nothing instructed
    return view.id


# --- the failure itself ---------------------------------------------------------------


def test_a_charged_deposit_goes_nowhere_on_its_own(setup) -> None:
    # The bug, stated as a test: with the depositor gone, nothing in the
    # system moves this deposit, and its voucher is already spent.
    service, _switch, provider, clock, factory, _user_id = setup
    deposit_id = _charge_a_deposit(setup)
    provider.online = True  # the provider is fine now; nobody is asking it
    clock.advance(STALE)  # the phone has been gone a while

    with factory() as session:
        assert session.get(Deposit, deposit_id).status is S.CHARGED
        voucher_pin = session.get(Deposit, deposit_id).voucher_pin
        assert session.get(Voucher, voucher_pin).status is VoucherStatus.REDEEMED


def test_nobody_else_can_rescue_it_through_the_app(setup) -> None:
    # Why a sweeper is needed rather than "someone else polls it":
    # get_deposit is scoped to the owner, so no other request can advance it.
    service, _switch, _provider, _clock, factory, _user_id = setup
    deposit_id = _charge_a_deposit(setup)
    with factory() as session:
        stranger = make_user(session, id_number="9202204720082")
        with pytest.raises(Refused) as refused:
            service.get_deposit(session, deposit_id, stranger)
    assert refused.value.reason == "deposit_not_found"


# --- the sweeper ----------------------------------------------------------------------


def test_the_sweeper_settles_a_deposit_its_depositor_abandoned(setup) -> None:
    service, _switch, provider, clock, factory, _user_id = setup
    deposit_id = _charge_a_deposit(setup)
    provider.online = True
    clock.advance(STALE)  # the phone has been gone a while

    with factory() as session:
        first = service.sweep(session, stale_after=timedelta(seconds=60))
    # One sweep instructs the payout; the provider reports it pending.
    assert (first.examined, first.settled) == (1, 0)
    assert first.unresolved == 1

    clock.advance(5)  # past the mock's completion time
    with factory() as session:
        second = service.sweep(session, stale_after=timedelta(seconds=60))
    assert (second.examined, second.settled, second.failed) == (1, 1, 0)

    with factory() as session:
        assert session.get(Deposit, deposit_id).status is S.SETTLED
        assert_ledger_balanced(session)


def test_the_sweeper_reverses_a_payout_the_bank_refused(setup) -> None:
    # A R405 voucher pays out 39487 cents, a mock failure trigger. The
    # customer must get her voucher back, not lose the cash to a dead end.
    service, _switch, provider, clock, factory, _user_id = setup
    deposit_id = _charge_a_deposit(setup, amount_cents=40500)
    provider.online = True
    clock.advance(STALE)  # the phone has been gone a while

    with factory() as session:
        service.sweep(session, stale_after=timedelta(seconds=60))
    clock.advance(5)
    with factory() as session:
        result = service.sweep(session, stale_after=timedelta(seconds=60))

    assert (result.failed, result.settled) == (1, 0)
    with factory() as session:
        deposit = session.get(Deposit, deposit_id)
        assert deposit.status is S.FAILED
        assert deposit.failure_reason == "bank_processing_error"
        # The voucher is spendable again, so "try again" works.
        assert session.get(Voucher, deposit.voucher_pin).status is VoucherStatus.ACTIVE
        assert_ledger_balanced(session)


def test_a_deposit_still_being_polled_is_left_alone(setup) -> None:
    # The sweeper is for deposits nobody is driving. One whose owner is
    # polling it right now has moved recently and is not stale.
    service, _switch, provider, _clock, factory, _user_id = setup
    _charge_a_deposit(setup)
    provider.online = True

    with factory() as session:
        result = service.sweep(session, stale_after=timedelta(seconds=60))
    assert result.examined == 0


def test_a_settled_deposit_is_never_looked_at_again(setup) -> None:
    service, _switch, provider, clock, factory, _user_id = setup
    deposit_id = _charge_a_deposit(setup)
    provider.online = True
    clock.advance(STALE)  # the phone has been gone a while
    with factory() as session:
        service.sweep(session, stale_after=timedelta(seconds=60))
    clock.advance(5)
    with factory() as session:
        service.sweep(session, stale_after=timedelta(seconds=60))

    with factory() as session:
        again = service.sweep(session, stale_after=timedelta(seconds=60))
    assert again.examined == 0  # settled is terminal; not a candidate


def test_sweeping_twice_changes_nothing_the_second_time(setup) -> None:
    # It is driven from outside by something that may retry or overlap, so
    # calling it again must never double-pay or double-post.
    service, _switch, provider, clock, factory, _user_id = setup
    deposit_id = _charge_a_deposit(setup)
    provider.online = True
    clock.advance(STALE)  # the phone has been gone a while
    with factory() as session:
        service.sweep(session, stale_after=timedelta(seconds=60))
    clock.advance(5)

    with factory() as session:
        service.sweep(session, stale_after=timedelta(seconds=60))
        payout_rows = session.scalars(select(Deposit.id)).all()
    with factory() as session:
        service.sweep(session, stale_after=timedelta(seconds=0))
        assert session.get(Deposit, deposit_id).status is S.SETTLED
        assert len(payout_rows) == 1
        assert_ledger_balanced(session)


def test_a_provider_still_down_leaves_the_deposit_for_next_time(setup) -> None:
    # Unreachable is not failure: nothing is known to have happened, so the
    # deposit must stay charged rather than be reversed on a guess.
    service, _switch, _provider, clock, factory, _user_id = setup
    deposit_id = _charge_a_deposit(setup)
    clock.advance(STALE)  # the phone has been gone a while

    with factory() as session:
        result = service.sweep(session, stale_after=timedelta(seconds=60))
    assert (result.examined, result.unresolved) == (1, 1)
    with factory() as session:
        assert session.get(Deposit, deposit_id).status is S.CHARGED


def test_a_sweep_is_bounded_and_takes_the_oldest_first(setup) -> None:
    service, _switch, provider, clock, factory, _user_id = setup
    ids = [_charge_a_deposit(setup) for _ in range(3)]
    provider.online = True
    clock.advance(STALE)  # the phone has been gone a while

    with factory() as session:
        result = service.sweep(session, stale_after=timedelta(seconds=60), limit=2)
    assert result.examined == 2

    with factory() as session:
        statuses = {d: session.get(Deposit, d).status for d in ids}
    assert statuses[ids[0]] is S.PAYING
    assert statuses[ids[1]] is S.PAYING
    assert statuses[ids[2]] is S.CHARGED  # over the limit, left for next time


def test_stranded_reports_what_is_waiting_without_touching_it(setup) -> None:
    service, _switch, _provider, clock, factory, _user_id = setup
    deposit_id = _charge_a_deposit(setup)
    clock.advance(STALE)  # the phone has been gone a while

    with factory() as session:
        waiting = service.stranded(session, stale_after=timedelta(seconds=60))
    assert waiting == [deposit_id]
    with factory() as session:
        assert session.get(Deposit, deposit_id).status is S.CHARGED


# --- the route that drives it ----------------------------------------------------------
#
# Nothing in the process runs on a timer, so the sweep is a request and
# whatever schedules it lives outside. See app/routes/internal.py.

SWEEP_KEY = "sweep-key-for-tests-only-0123456789abcdef"
SWEEP_HEADER = {"X-Sweep-Key": SWEEP_KEY}


@pytest.fixture
def sweep_api(test_settings, clean_db: Engine):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = test_settings.model_copy(update={"sweep_key": SWEEP_KEY})
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def no_sweep_api(test_settings, clean_db: Engine):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = test_settings.model_copy(update={"sweep_key": None})
    with TestClient(create_app(settings)) as client:
        yield client


def test_without_a_key_configured_the_route_does_not_exist(no_sweep_api) -> None:
    # An open sweep endpoint would let anyone drive our payout provider, so
    # no key means no route at all rather than an unguarded one.
    response = no_sweep_api.post("/internal/sweep")
    assert (response.status_code, response.json()) == (404, {"detail": "Not Found"})


@pytest.mark.parametrize(
    "headers", [{}, {"X-Sweep-Key": "wrong"}, {"X-Sweep-Key": SWEEP_KEY[:-1]}]
)
def test_without_the_key_the_route_looks_like_it_does_not_exist(
    sweep_api, headers: dict[str, str]
) -> None:
    response = sweep_api.post("/internal/sweep", headers=headers)
    assert (response.status_code, response.json()) == (404, {"detail": "Not Found"})
    # Byte-identical to a genuine 404, so the route cannot be discovered.
    assert sweep_api.get("/no-such-path").json() == {"detail": "Not Found"}


def test_with_the_key_it_reports_what_it_did(sweep_api) -> None:
    response = sweep_api.post("/internal/sweep", headers=SWEEP_HEADER)
    assert response.status_code == 200
    assert response.json() == {
        "examined": 0,
        "settled": 0,
        "failed": 0,
        "unresolved": 0,
        "skipped": 0,
        "more_likely": False,
    }


def test_the_route_is_left_out_of_the_openapi_schema(sweep_api) -> None:
    paths = sweep_api.get("/openapi.json").json()["paths"]
    assert not any(path.startswith("/internal") for path in paths)


def test_the_answer_carries_no_deposit_details(sweep_api) -> None:
    # It goes to whatever is scheduling the call, which has no business
    # seeing deposit ids, destinations or amounts.
    body = sweep_api.post("/internal/sweep", headers=SWEEP_HEADER).json()
    assert set(body) == {"examined", "settled", "failed", "unresolved", "skipped", "more_likely"}


@pytest.mark.parametrize(
    "query", ["?limit=0", "?limit=201", "?stale_after_seconds=0", "?stale_after_seconds=86401"]
)
def test_the_route_refuses_an_unreasonable_budget(sweep_api, query: str) -> None:
    assert sweep_api.post(f"/internal/sweep{query}", headers=SWEEP_HEADER).status_code == 422
