"""Database-level rules for voucher tokens, deposits, the ledger and payouts.

Raw SQL on purpose, as in test_schema.py: these check what Postgres itself
rejects, independent of the application code.
"""

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

PIN = "1234567890123456"
OTHER_PIN = "6543210987654321"

INSERT_VOUCHER = text(
    "INSERT INTO vouchers (pin, amount_cents, status, redeemed_at, serial)"
    " VALUES (:pin, 50000, 'redeemed', now(), '20260924' || left(:pin, 12))"
)
INSERT_DEPOSIT = text(
    "INSERT INTO deposits (id, reference, voucher_pin, amount_cents, fee_cents, payout_cents,"
    " destination, status, failure_reason, idempotency_key)"
    " VALUES (:id, :reference, :voucher_pin, :amount_cents, :fee_cents, :payout_cents,"
    " cast(:destination AS jsonb), :status, :failure_reason, :idempotency_key)"
)
INSERT_ENTRY = text(
    "INSERT INTO ledger_entries (deposit_id, account, amount_cents, entry_group)"
    " VALUES (:deposit_id, :account, :amount_cents, :entry_group)"
)
INSERT_PAYOUT = text(
    "INSERT INTO payouts (deposit_id, provider_ref, amount_cents, status, failure_reason)"
    " VALUES (:deposit_id, :provider_ref, 49500, 'pending', NULL)"
)


def _voucher(session: Session, pin: str = PIN) -> None:
    session.execute(INSERT_VOUCHER, {"pin": pin})


def _deposit(session: Session, **overrides: Any) -> uuid.UUID:
    row: dict[str, Any] = {
        "id": uuid.uuid4(),
        "reference": "KD-7F3A9C",
        "voucher_pin": PIN,
        "amount_cents": 50000,
        "fee_cents": 500,
        "payout_cents": 49500,
        "destination": json.dumps({"kind": "shap_id", "shap_id": "+27821234560"}),
        "status": "charged",
        "failure_reason": None,
        "idempotency_key": str(uuid.uuid4()),
    }
    row.update(overrides)
    session.execute(INSERT_DEPOSIT, row)
    deposit_id: uuid.UUID = row["id"]
    return deposit_id


def _entries(session: Session, deposit_id: uuid.UUID | None, *lines: tuple[str, int]) -> uuid.UUID:
    group = uuid.uuid4()
    for account, amount in lines:
        session.execute(
            INSERT_ENTRY,
            {"deposit_id": deposit_id, "account": account, "amount_cents": amount, "entry_group": group},
        )
    return group


CHARGE = (("voucher_receivable", 50000), ("user_payable", -49500), ("fee_income", -500))


def test_a_valid_token_deposit_ledger_and_payout_are_accepted(db_session: Session) -> None:
    _voucher(db_session)
    db_session.execute(
        text(
            "INSERT INTO voucher_tokens (token, voucher_pin, expires_at)"
            " VALUES ('tok', :pin, now() + interval '5 minutes')"
        ),
        {"pin": PIN},
    )
    deposit_id = _deposit(db_session)
    _entries(db_session, deposit_id, *CHARGE)
    _entries(db_session, None, ("settlement", 1_000_000), ("capital", -1_000_000))
    db_session.execute(INSERT_PAYOUT, {"deposit_id": deposit_id, "provider_ref": "po_1"})
    db_session.commit()


# --- deposits -------------------------------------------------------------------


def test_payout_must_be_amount_less_fee(db_session: Session) -> None:
    _voucher(db_session)
    with pytest.raises(IntegrityError, match="ck_deposits_payout_is_amount_less_fee"):
        _deposit(db_session, payout_cents=49000)


@pytest.mark.parametrize(
    ("status", "failure_reason"), [("failed", None), ("charged", "bank_unavailable")]
)
def test_failure_reason_is_set_exactly_when_failed(
    db_session: Session, status: str, failure_reason: str | None
) -> None:
    _voucher(db_session)
    with pytest.raises(IntegrityError, match="ck_deposits_failure_reason_iff_failed"):
        _deposit(db_session, status=status, failure_reason=failure_reason)


@pytest.mark.parametrize("reference", ["KD-7F3A9O", "KD-7F3A91", "KD-7F3A9I", "KD-7f3a9c", "KD-7F3A9", "XX-7F3A9C"])
def test_reference_format_excludes_ambiguous_characters(db_session: Session, reference: str) -> None:
    _voucher(db_session)
    with pytest.raises(IntegrityError, match="ck_deposits_reference_format"):
        _deposit(db_session, reference=reference)


def test_destination_must_say_its_kind(db_session: Session) -> None:
    _voucher(db_session)
    with pytest.raises(IntegrityError, match="ck_deposits_destination_has_kind"):
        _deposit(db_session, destination=json.dumps({"shap_id": "+27821234560"}))


@pytest.mark.parametrize(
    ("column", "constraint"),
    [("reference", "uq_deposits_reference"), ("idempotency_key", "uq_deposits_idempotency_key")],
)
def test_reference_and_idempotency_key_are_unique(
    db_session: Session, column: str, constraint: str
) -> None:
    _voucher(db_session)
    _deposit(db_session, reference="KD-AAAAAA", idempotency_key="key-1")
    duplicate = {"reference": "KD-BBBBBB", "idempotency_key": "key-2", column: "KD-AAAAAA" if column == "reference" else "key-1"}
    with pytest.raises(IntegrityError, match=constraint):
        _deposit(db_session, **duplicate)


def test_deposit_must_reference_a_real_voucher(db_session: Session) -> None:
    with pytest.raises(IntegrityError, match="fk_deposits_voucher_pin_vouchers"):
        _deposit(db_session)


def test_updated_at_is_maintained_by_the_database(db_session: Session) -> None:
    _voucher(db_session)
    deposit_id = _deposit(db_session)
    db_session.commit()
    db_session.execute(
        text("UPDATE deposits SET status = 'paying' WHERE id = :id"), {"id": deposit_id}
    )
    db_session.commit()
    created, updated = db_session.execute(
        text("SELECT created_at, updated_at FROM deposits WHERE id = :id"), {"id": deposit_id}
    ).one()
    assert updated > created


# --- ledger ---------------------------------------------------------------------


def test_zero_amount_entries_are_rejected(db_session: Session) -> None:
    with pytest.raises(IntegrityError, match="ck_ledger_entries_amount_cents_non_zero"):
        _entries(db_session, None, ("settlement", 0))


def test_only_funding_entries_may_omit_the_deposit(db_session: Session) -> None:
    _entries(db_session, None, ("settlement", 500), ("capital", -500))
    with pytest.raises(IntegrityError, match="ck_ledger_entries_only_funding_without_deposit"):
        _entries(db_session, None, ("user_payable", -500))


def test_an_unbalanced_entry_group_cannot_be_committed(db_session: Session) -> None:
    _voucher(db_session)
    deposit_id = _deposit(db_session)
    _entries(db_session, deposit_id, ("voucher_receivable", 50000), ("user_payable", -49500))
    with pytest.raises(IntegrityError, match="does not sum to zero"):
        db_session.commit()


def test_a_lone_funding_entry_without_a_counterparty_cannot_be_committed(
    db_session: Session,
) -> None:
    # The CHECK lets funding entries omit deposit_id, so the zero-sum trigger is
    # the only thing stopping a float top-up that creates money from nothing.
    _entries(db_session, None, ("settlement", 1_000_000))
    with pytest.raises(IntegrityError, match="does not sum to zero"):
        db_session.commit()


def test_a_balanced_funding_entry_without_a_deposit_commits(db_session: Session) -> None:
    group = _entries(db_session, None, ("settlement", 1_000_000), ("capital", -1_000_000))
    db_session.commit()
    total = db_session.execute(
        text("SELECT sum(amount_cents) FROM ledger_entries WHERE entry_group = :g"), {"g": group}
    ).scalar_one()
    assert total == 0


def _committed_entry(db_session: Session) -> int:
    group = _entries(db_session, None, ("settlement", 500), ("capital", -500))
    db_session.commit()
    entry_id: int = db_session.execute(
        text("SELECT min(id) FROM ledger_entries WHERE entry_group = :g"), {"g": group}
    ).scalar_one()
    return entry_id


def test_an_update_on_ledger_entries_is_rejected(db_session: Session) -> None:
    entry_id = _committed_entry(db_session)
    with pytest.raises(DBAPIError, match="append-only: UPDATE is not allowed"):
        db_session.execute(
            text("UPDATE ledger_entries SET amount_cents = 1 WHERE id = :id"), {"id": entry_id}
        )


def test_a_delete_on_ledger_entries_is_rejected(db_session: Session) -> None:
    entry_id = _committed_entry(db_session)
    with pytest.raises(DBAPIError, match="append-only: DELETE is not allowed"):
        db_session.execute(text("DELETE FROM ledger_entries WHERE id = :id"), {"id": entry_id})


# --- payouts and tokens -----------------------------------------------------------


def test_one_payout_per_deposit_with_a_unique_provider_ref(db_session: Session) -> None:
    _voucher(db_session)
    _voucher(db_session, OTHER_PIN)
    first = _deposit(db_session)
    second = _deposit(db_session, voucher_pin=OTHER_PIN, reference="KD-BBBBBB")
    db_session.execute(INSERT_PAYOUT, {"deposit_id": first, "provider_ref": "po_1"})
    with pytest.raises(IntegrityError, match="uq_payouts_provider_ref"):
        db_session.execute(INSERT_PAYOUT, {"deposit_id": second, "provider_ref": "po_1"})


def test_a_second_payout_for_the_same_deposit_is_rejected(db_session: Session) -> None:
    _voucher(db_session)
    deposit_id = _deposit(db_session)
    db_session.execute(INSERT_PAYOUT, {"deposit_id": deposit_id, "provider_ref": "po_1"})
    with pytest.raises(IntegrityError, match="pk_payouts"):
        db_session.execute(INSERT_PAYOUT, {"deposit_id": deposit_id, "provider_ref": "po_2"})


def test_voucher_token_must_reference_a_real_voucher(db_session: Session) -> None:
    with pytest.raises(IntegrityError, match="fk_voucher_tokens_voucher_pin_vouchers"):
        db_session.execute(
            text(
                "INSERT INTO voucher_tokens (token, voucher_pin, expires_at)"
                " VALUES ('tok', '0000000000000000', now())"
            )
        )
