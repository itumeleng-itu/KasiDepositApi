"""Deposits: everything between "the app sends a PIN" and "the money has landed".

The flow
--------
1. Lookup (`lookup_voucher`). The app sends the PIN once. We report the amount,
   fee and payout, and hand back an opaque, short-lived, single-use voucher
   token. The PIN is never needed again.

2. Create (`create_deposit`), ONE database transaction up to the ledger post:
   resolve the destination first (a ShapID failure means nothing was
   charged); take the float lock; lock the token; refuse a voucher too small
   to deposit; refuse a payout the float cannot cover (`insufficient_float`,
   nothing charged, so "try again later" is true); charge the voucher under
   its row lock; insert the deposit (`pending`), move it to `charged` and post
   the charge entries; mark the token used; commit.

   Lock order is always: float lock, then token, then the voucher row, then
   the new deposit. See `lock_and_check_float` and `VoucherSwitch.charge`.

3. Instruct the payout, after that commit. If the provider cannot be reached
   the deposit simply stays `charged`, and the next status read instructs it.

4. Status (`get_deposit`). The app polls every second. A settled or failed
   deposit is one primary-key read. An in-flight one is advanced lazily,
   under a row lock on the deposit so two polls cannot both settle it:
   `charged` with no payout gets its payout instructed; `paying` asks the
   provider and, on completion, posts the settlement entries and settles; on
   failure it reverses the charge (below) and fails. A provider that is slow
   or unreachable is never a failure: the deposit stays where it is.

Idempotency
-----------
The same Idempotency-Key returns the same deposit (200, not 201). It is
enforced by the database, not by checking first: a replay shows up as a
used token, an already-redeemed voucher, or a unique violation on
`deposits.idempotency_key`, and only then do we look the key up. A different
key for an already-used voucher is `voucher_already_redeemed`.

A failed payout
---------------
The charge is reversed: compensating entries undo all three charge lines,
and the voucher returns to active, so the app's "Try again" with the same PIN
works instead of stranding her money. This is a DEMO SIMPLIFICATION: a real
issuer would need a reversal API, and may not expose one.

States
------
    pending -> charged -> paying -> settled
    pending, charged, paying -> failed

Every status change goes through `transition`, which refuses anything else.
The app sees four statuses: pending and charged as "pending", paying as
"submitted", settled as "completed", failed as "failed".
"""

import re
import secrets
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.errors import UniqueViolation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.banks import BANK_IDS_BY_API_CODE
from app.ledger import Entry, InsufficientFloat, check_float, lock_float, post
from app.models import (
    Deposit,
    DepositStatus,
    LedgerAccount,
    Payout,
    PayoutStatus,
    VoucherStatus,
    VoucherToken,
)
from app.money import Cents
from app.payouts import PayoutProvider, PayoutRef, PayoutUnavailable
from app.shapid import ShapIdError, resolve
from app.switch import AlreadyRedeemed, VoucherNotFound, VoucherSwitch

S = DepositStatus

ALLOWED_TRANSITIONS: frozenset[tuple[DepositStatus, DepositStatus]] = frozenset(
    {
        (S.PENDING, S.CHARGED),
        (S.CHARGED, S.PAYING),
        (S.PAYING, S.SETTLED),
        (S.PENDING, S.FAILED),
        (S.CHARGED, S.FAILED),
        (S.PAYING, S.FAILED),
    }
)

APP_STATUS: dict[DepositStatus, str] = {
    S.PENDING: "pending",
    S.CHARGED: "pending",
    S.PAYING: "submitted",
    S.SETTLED: "completed",
    S.FAILED: "failed",
}

REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no O/0, no I/1
REFERENCE_LENGTH = 6
MAX_REFERENCE_ATTEMPTS = 5
_PIN = re.compile(r"^[0-9]{16}$")
_ACCOUNT_NUMBER = re.compile(r"^[0-9]{6,16}$")


class IllegalTransition(RuntimeError):
    pass


class Refused(Exception):
    """A request the API declines. `reason` is the exact string the app expects."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Replay(Exception):
    """The voucher or token is already used: either this is a replay of an
    earlier request (same Idempotency-Key) or the voucher is spent."""


def transition(deposit: Deposit, to: DepositStatus, failure_reason: str | None = None) -> None:
    """The only way a deposit's status changes. Illegal moves raise, never write."""
    if (deposit.status, to) not in ALLOWED_TRANSITIONS:
        raise IllegalTransition(f"{deposit.status} -> {to} is not allowed")
    if (to is S.FAILED) != (failure_reason is not None):
        raise ValueError("a failure reason is required for failed, and only for failed")
    deposit.status = to
    deposit.failure_reason = failure_reason


def new_reference() -> str:
    return "KD-" + "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(REFERENCE_LENGTH))


@dataclass(frozen=True)
class VoucherLookup:
    voucher_token: str
    value_cents: Cents
    fee_cents: Cents
    payout_cents: Cents


@dataclass(frozen=True)
class DepositView:
    id: uuid.UUID
    reference: str
    status: str  # as the app knows it
    payout_cents: Cents
    failure_reason: str | None

    @classmethod
    def of(cls, deposit: Deposit) -> "DepositView":
        return cls(
            id=deposit.id,
            reference=deposit.reference,
            status=APP_STATUS[deposit.status],
            payout_cents=deposit.payout_cents,
            failure_reason=deposit.failure_reason,
        )


def _is_unique_violation(exc: IntegrityError, constraint: str) -> bool:
    return isinstance(exc.orig, UniqueViolation) and exc.orig.diag.constraint_name == constraint


class DepositService:
    def __init__(
        self,
        switch: VoucherSwitch,
        provider: PayoutProvider,
        fee_cents: Cents,
        min_voucher_cents: Cents,
        token_ttl: timedelta,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.switch = switch
        self.provider = provider
        self.fee_cents = fee_cents
        self.min_voucher_cents = min_voucher_cents
        self.token_ttl = token_ttl
        self.now = now

    # --- rules -------------------------------------------------------------

    def is_too_small(self, amount_cents: Cents) -> bool:
        """Below the minimum, or nothing left after the fee. Unreachable while
        vending enforces MIN_VOUCHER_CENTS above the fee; kept so a change to
        either surfaces it rather than paying out nothing."""
        return amount_cents < self.min_voucher_cents or amount_cents <= self.fee_cents

    # --- lookup ------------------------------------------------------------

    def lookup_voucher(self, session: Session, pin: str) -> VoucherLookup:
        if not _PIN.fullmatch(pin):
            raise Refused("voucher_not_found")
        try:
            info = self.switch.lookup(session, pin)
        except VoucherNotFound:
            raise Refused("voucher_not_found") from None
        if info.status is not VoucherStatus.ACTIVE:
            raise Refused("voucher_already_redeemed")
        token = secrets.token_urlsafe(32)
        session.add(
            VoucherToken(token=token, voucher_pin=pin, expires_at=self.now() + self.token_ttl)
        )
        session.commit()
        return VoucherLookup(
            voucher_token=token,
            value_cents=info.amount_cents,
            fee_cents=self.fee_cents,
            payout_cents=max(info.amount_cents - self.fee_cents, 0),
        )

    # --- create ------------------------------------------------------------

    def create_deposit(
        self,
        session: Session,
        voucher_token: str,
        destination: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[DepositView, bool]:
        """Returns the deposit and whether it was created (False: a replay)."""
        stored_destination = self._resolve_destination(destination)
        try:
            deposit = self._charge_and_record(
                session, voucher_token, stored_destination, idempotency_key
            )
            session.commit()
        except _Replay:
            return self._replay(session, idempotency_key), False
        except IntegrityError as exc:
            if not _is_unique_violation(exc, "uq_deposits_idempotency_key"):
                raise
            return self._replay(session, idempotency_key), False
        except Refused:
            session.rollback()
            raise

        self._instruct_payout(session, deposit.id)
        return DepositView.of(self._load(session, deposit.id)), True

    def _replay(self, session: Session, idempotency_key: str) -> DepositView:
        session.rollback()
        existing = session.scalar(
            select(Deposit)
            .where(Deposit.idempotency_key == idempotency_key)
            .execution_options(populate_existing=True)
        )
        if existing is None:
            raise Refused("voucher_already_redeemed")
        return DepositView.of(existing)

    def _charge_and_record(
        self,
        session: Session,
        voucher_token: str,
        destination: dict[str, Any],
        idempotency_key: str,
    ) -> Deposit:
        lock_float(session)

        token = session.scalar(
            select(VoucherToken)
            .where(VoucherToken.token == voucher_token)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if token is None:
            raise Refused("voucher_not_found")
        if token.consumed_at is not None:  # before expiry: a late replay still finds its deposit
            raise _Replay()
        if token.expires_at <= self.now():
            raise Refused("voucher_not_found")

        info = self.switch.lookup(session, token.voucher_pin)
        if info.status is not VoucherStatus.ACTIVE:
            raise _Replay()
        if self.is_too_small(info.amount_cents):
            raise Refused("voucher_too_small")
        payout_cents = info.amount_cents - self.fee_cents
        try:
            check_float(session, payout_cents)
        except InsufficientFloat as exc:
            raise Refused(exc.reason) from exc

        try:
            charged = self.switch.charge(session, token.voucher_pin)
        except AlreadyRedeemed:
            raise _Replay() from None

        deposit = self._insert_deposit(
            session,
            voucher_pin=charged.pin,
            amount_cents=charged.amount_cents,
            payout_cents=charged.amount_cents - self.fee_cents,
            destination=destination,
            idempotency_key=idempotency_key,
        )
        transition(deposit, S.CHARGED)
        post(
            session,
            deposit.id,
            [
                Entry(LedgerAccount.VOUCHER_RECEIVABLE, deposit.amount_cents),
                Entry(LedgerAccount.USER_PAYABLE, -deposit.payout_cents),
                Entry(LedgerAccount.FEE_INCOME, -deposit.fee_cents),
            ],
        )
        token.consumed_at = self.now()
        session.flush()
        return deposit

    def _insert_deposit(
        self,
        session: Session,
        *,
        voucher_pin: str,
        amount_cents: Cents,
        payout_cents: Cents,
        destination: dict[str, Any],
        idempotency_key: str,
    ) -> Deposit:
        """Insert as `pending`. A reference collision retries with a fresh one
        inside a savepoint; any other violation (the idempotency key) propagates."""
        for _ in range(MAX_REFERENCE_ATTEMPTS):
            deposit = Deposit(
                reference=new_reference(),
                voucher_pin=voucher_pin,
                amount_cents=amount_cents,
                fee_cents=self.fee_cents,
                payout_cents=payout_cents,
                destination=destination,
                status=S.PENDING,
                idempotency_key=idempotency_key,
            )
            try:
                with session.begin_nested():
                    session.add(deposit)
            except IntegrityError as exc:
                if _is_unique_violation(exc, "uq_deposits_reference"):
                    continue
                raise
            return deposit
        raise RuntimeError(f"no unique reference after {MAX_REFERENCE_ATTEMPTS} attempts")

    def _resolve_destination(self, destination: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and resolve before anything is charged. Returns what is stored."""
        kind = destination.get("kind")
        if kind == "shap_id":
            shap_id = destination.get("shap_id")
            if not isinstance(shap_id, str):
                raise Refused("shapid_invalid_format")
            try:
                resolved = resolve(shap_id)
            except ShapIdError as exc:
                raise Refused(exc.reason) from None
            return {"kind": "shap_id", "shap_id": shap_id, "bank_id": resolved.bank_id}
        if kind == "account":
            # POPIA: see Deposit.destination. Nothing constructs this kind today.
            name = destination.get("name")
            number = destination.get("account_number")
            bank = BANK_IDS_BY_API_CODE.get(str(destination.get("bank")))
            if (
                not isinstance(name, str)
                or not name.strip()
                or not isinstance(number, str)
                or not _ACCOUNT_NUMBER.fullmatch(number)
                or bank is None
            ):
                raise Refused("invalid_destination")
            return {"kind": "account", "name": name, "account_number": number, "bank_id": bank}
        raise Refused("invalid_destination")

    # --- payout ------------------------------------------------------------

    def _lock(self, session: Session, deposit_id: uuid.UUID) -> Deposit | None:
        return session.scalar(
            select(Deposit)
            .where(Deposit.id == deposit_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def _load(self, session: Session, deposit_id: uuid.UUID) -> Deposit:
        deposit = session.scalar(
            select(Deposit).where(Deposit.id == deposit_id).execution_options(populate_existing=True)
        )
        if deposit is None:
            raise Refused("deposit_not_found")
        return deposit

    def _instruct_payout(self, session: Session, deposit_id: uuid.UUID) -> None:
        """Instruct the payout for a `charged` deposit and move it to `paying`.
        If the provider cannot be reached, leave it `charged` for next time."""
        deposit = self._lock(session, deposit_id)
        if deposit is None or deposit.status is not S.CHARGED:
            session.rollback()
            return
        try:
            ref = self.provider.create(
                deposit.payout_cents, deposit.destination, idempotency_key=str(deposit.id)
            )
        except PayoutUnavailable:
            session.rollback()
            return
        session.add(
            Payout(
                deposit_id=deposit.id,
                provider_ref=ref.provider_ref,
                amount_cents=deposit.payout_cents,
                status=PayoutStatus.PENDING,
            )
        )
        transition(deposit, S.PAYING)
        session.commit()

    def _advance_payout(self, session: Session, deposit_id: uuid.UUID) -> None:
        deposit = self._lock(session, deposit_id)
        if deposit is None or deposit.status is not S.PAYING:
            session.rollback()
            return
        payout = session.get(Payout, deposit.id, populate_existing=True)
        if payout is None:  # cannot happen: paying is only reached with a payout row
            session.rollback()
            return
        try:
            result = self.provider.status(PayoutRef(payout.provider_ref))
        except PayoutUnavailable:
            session.rollback()
            return

        if result.status in (PayoutStatus.PENDING, PayoutStatus.SUBMITTED):
            if payout.status is not result.status:
                payout.status = result.status
                session.commit()
            else:
                session.rollback()
            return

        payout.status = result.status
        if result.status is PayoutStatus.COMPLETED:
            post(
                session,
                deposit.id,
                [
                    Entry(LedgerAccount.USER_PAYABLE, deposit.payout_cents),
                    Entry(LedgerAccount.SETTLEMENT, -deposit.payout_cents),
                ],
            )
            transition(deposit, S.SETTLED)
        else:
            reason = result.failure_reason or "unknown"
            payout.failure_reason = reason
            # Reverse the charge: undo all three charge lines, and return the
            # voucher to active (a demo simplification; see the module docstring).
            post(
                session,
                deposit.id,
                [
                    Entry(LedgerAccount.VOUCHER_RECEIVABLE, -deposit.amount_cents),
                    Entry(LedgerAccount.USER_PAYABLE, deposit.payout_cents),
                    Entry(LedgerAccount.FEE_INCOME, deposit.fee_cents),
                ],
            )
            self.switch.reverse_charge(session, deposit.voucher_pin)
            transition(deposit, S.FAILED, reason)
        session.commit()

    # --- status ------------------------------------------------------------

    def get_deposit(self, session: Session, deposit_id: uuid.UUID) -> DepositView:
        deposit = self._load(session, deposit_id)
        if deposit.status is S.CHARGED:
            session.rollback()
            self._instruct_payout(session, deposit_id)
            deposit = self._load(session, deposit_id)
        if deposit.status is S.PAYING:
            session.rollback()
            self._advance_payout(session, deposit_id)
            deposit = self._load(session, deposit_id)
        view = DepositView.of(deposit)
        session.rollback()  # end the read transaction; never leave it idle
        return view
