"""Tracing a voucher to its deposit, and back.

The complaint this exists for: someone comes to the shop saying the
voucher did not work, or that they redeemed it and the money never
came. Before this, the two sides shared no visible identifier — the
slip carries a serial the app never shows, the app carries a KD-
reference the till could not search — and the till's knowledge stopped
at "used", which is no answer to "where is my money".
"""

import time
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.db import make_session_factory
from app.deposits import DepositService
from app.ledger import fund_float
from app.payouts import MockPayoutProvider
from app.models import Voucher, VoucherStatus
from app.switch import VoucherSwitch
from tests.conftest import make_user, vend_headers

DESTINATION = {"kind": "shap_id", "shap_id": "+27821234560"}


@pytest.fixture
def made(clean_db: Engine):
    """A redeemed voucher and the deposit it became."""
    switch = VoucherSwitch(1000, 500000)
    service = DepositService(
        switch=switch,
        provider=MockPayoutProvider(),
        fee_basis_points=250,
        min_voucher_cents=1000,
        token_ttl=timedelta(minutes=10),
    )
    factory = make_session_factory(clean_db)
    with factory() as session:
        fund_float(session, 1_000_000)
        vended = switch.vend(session, 50000)
        session.commit()
        user_id = make_user(session)
        token = service.lookup_voucher(session, vended.pin).voucher_token
        view, _ = service.create_deposit(
            session, token, DESTINATION, str(uuid.uuid4()), user_id
        )
    return service, factory, vended, view, user_id


# --- slip -> what became of the money -------------------------------------------------


def test_a_serial_finds_the_deposit_it_became(made) -> None:
    service, factory, vended, view, _user_id = made
    with factory() as session:
        traces = service.traces_for_vouchers(session, [vended.serial])

    assert vended.serial in traces
    assert traces[vended.serial].reference == view.reference
    assert traces[vended.serial].attempts == 1


def test_a_trace_says_nothing_about_where_the_money_went(made) -> None:
    # The till belongs to the shop. A shop must not learn where its
    # customer banks, so a trace carries no destination, account, holder
    # name, user or fee -- only enough to settle a dispute.
    service, factory, vended, _view, _user_id = made
    with factory() as session:
        trace = service.traces_for_vouchers(session, [vended.serial])[vended.serial]

    leaked = {"shap_id", "shap_name", "account_last4", "account_holder", "user_id",
              "destination", "bank", "bank_id", "fee_cents"}
    assert leaked.isdisjoint(vars(trace))
    assert set(vars(trace)) == {"reference", "outcome", "attempts", "at"}


def test_an_unredeemed_voucher_has_no_trace(made) -> None:
    service, factory, _vended, _view, _user_id = made
    switch = VoucherSwitch(1000, 500000)
    with factory() as session:
        unsold = switch.vend(session, 10000)
        session.commit()
        traces = service.traces_for_vouchers(session, [unsold.serial])
    assert traces == {}


def test_asking_about_no_serials_asks_the_database_nothing(made) -> None:
    service, factory, _vended, _view, _user_id = made
    with factory() as session:
        assert service.traces_for_vouchers(session, []) == {}


# --- phone -> the slip it came from ---------------------------------------------------


def test_history_names_the_slip_the_deposit_came_from(made) -> None:
    service, factory, vended, _view, user_id = made
    with factory() as session:
        records = service.list_deposits(session, user_id, 20)

    assert len(records) == 1
    assert records[0].voucher_serial == vended.serial


# --- through the API ------------------------------------------------------------------


def test_the_till_list_carries_the_deposit_trace(client: TestClient, clean_db: Engine) -> None:
    sold = client.post(
        "/demo/vouchers", json={"amount_cents": 50000}, headers=vend_headers()
    ).json()
    items = client.get("/demo/vouchers").json()
    mine = next(i for i in items if i["serial"] == sold["serial"])
    # Not redeemed yet: nothing to trace.
    assert mine["deposit"] is None

    factory = make_session_factory(clean_db)
    switch = VoucherSwitch(1000, 500000)
    service = DepositService(
        switch=switch,
        provider=MockPayoutProvider(),
        fee_basis_points=250,
        min_voucher_cents=1000,
        token_ttl=timedelta(minutes=10),
    )
    with factory() as session:
        fund_float(session, 1_000_000)
        session.commit()
        user_id = make_user(session)
        token = service.lookup_voucher(session, sold["pin"]).voucher_token
        view, _ = service.create_deposit(
            session, token, DESTINATION, str(uuid.uuid4()), user_id
        )

    items = client.get("/demo/vouchers").json()
    mine = next(i for i in items if i["serial"] == sold["serial"])
    assert mine["deposit"]["reference"] == view.reference
    assert mine["deposit"]["outcome"] in {"in_progress", "paid", "failed"}
    assert mine["deposit"]["attempts"] == 1


def test_the_till_list_never_exposes_a_destination(client: TestClient, clean_db: Engine) -> None:
    sold = client.post(
        "/demo/vouchers", json={"amount_cents": 50000}, headers=vend_headers()
    ).json()
    body = client.get("/demo/vouchers").text
    assert sold["serial"] in body
    for secret in ("+27821234560", "shap_name", "account_last4", "account_holder"):
        assert secret not in body


# --- the voucher that was tried twice -------------------------------------------------


def test_a_retried_voucher_reports_the_latest_attempt_and_counts_them(clean_db: Engine) -> None:
    """A failed payout puts the voucher back, so she can try again — and
    then one voucher has two deposits. The cashier must see the attempt
    that matters (the last one) and know there were others, or a customer
    waving an older reference gets told the wrong thing."""
    switch = VoucherSwitch(1000, 500000)
    service = DepositService(
        switch=switch,
        provider=MockPayoutProvider(),
        fee_basis_points=250,
        min_voucher_cents=1000,
        token_ttl=timedelta(minutes=10),
    )
    factory = make_session_factory(clean_db)
    with factory() as session:
        fund_float(session, 1_000_000)
        # R405 pays out 39487c, a mock failure trigger: the payout fails and
        # reverse_charge returns the voucher to active.
        vended = switch.vend(session, 40500)
        session.commit()
        user_id = make_user(session)

        token = service.lookup_voucher(session, vended.pin).voucher_token
        first, _ = service.create_deposit(
            session, token, DESTINATION, str(uuid.uuid4()), user_id
        )
    # Drive it to failure the way the app would, by reading its status.
    deadline = time.time() + 20
    while time.time() < deadline:
        with factory() as session:
            view = service.get_deposit(session, first.id, user_id)
        if view.status in {"completed", "failed"}:
            break
        time.sleep(0.5)
    assert view.status == "failed", "the trigger amount should fail the payout"

    with factory() as session:
        assert session.get(Voucher, vended.pin).status is VoucherStatus.ACTIVE
        token = service.lookup_voucher(session, vended.pin).voucher_token
        second, _ = service.create_deposit(
            session, token, DESTINATION, str(uuid.uuid4()), user_id
        )
        traces = service.traces_for_vouchers(session, [vended.serial])

    trace = traces[vended.serial]
    assert trace.attempts == 2
    # The latest, not the first: she is standing there about this one.
    assert trace.reference == second.reference
    assert trace.reference != first.reference
