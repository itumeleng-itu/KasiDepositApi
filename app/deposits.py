"""Deposits: everything between "the app sends a PIN" and "the money has landed".

The flow
--------
1. Lookup (`lookup_voucher`). The app sends the PIN once. We report the amount,
   fee and payout, and hand back an opaque, short-lived, single-use voucher
   token. The PIN is never needed again.

2. Risk (`_screen`, app/risk.py), BEFORE anything is locked or charged: the
   facts are read in a short transaction that is ended before the decision
   is asked for. `review` is refused (deposit_declined); `warn` is refused
   (confirmation_required) unless the request carries risk_acknowledged.
   Nothing is charged either way. If the assessment fails, it FAILS OPEN:
   the deposit proceeds.

   Create (`create_deposit`), ONE database transaction up to the ledger post:
   resolve the destination first (a ShapID failure means nothing was
   charged); take the float lock; lock the token; refuse a voucher too small
   to deposit; refuse a payout the float cannot cover (`insufficient_float`,
   nothing charged, so "try again later" is true); charge the voucher under
   its row lock; insert the deposit (`pending`), move it to `charged` and post
   the charge entries; mark the token used; commit.

   Lock order is always: float lock, then token, then the voucher row, then
   the deposit. See `lock_and_check_float` and `VoucherSwitch.charge`.

3. Instruct the payout, after that commit (`advance`). If the provider cannot
   be reached the deposit simply stays `charged` for the next advance.

4. Advance (`advance`), the one code path for moving a deposit on, called by
   the status read (`get_deposit`, the app polls every second) and, when
   PAYOUT_WORKER_ENABLED, by the background worker (app/worker.py) for
   deposits nobody is polling. `charged` gets its payout instructed; `paying`
   asks the provider and, on completion, posts the settlement entries and
   settles; on failure it reverses the charge (below) and fails. The provider
   is always called with no transaction open, and every write re-checks the
   status under the deposit's row lock; see the comment above `advance` for
   why a crash or a race can never pay or post twice. A provider that is
   slow or unreachable is never a failure: the deposit stays where it is.

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

Ownership
---------
Every deposit belongs to the user who created it (`user_id`). Reading or
replaying another user's deposit is `deposit_not_found`, never 403, so ids
cannot be probed. Listing a user's deposits (`list_deposits`) is read-only: it
never advances a payout; `get_deposit` and the worker do, through `advance`.

Account numbers
---------------
An "account" destination's number is encrypted before it is stored
(app/pii.py) and decrypted only to instruct the payout. Without PII_KEY the
account path is refused rather than stored in plaintext.
"""

import base64
import logging
import re
import secrets
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.errors import UniqueViolation
from sqlalchemy import select, text
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
from app.money import Cents, percent_of
from app.payouts import PayoutProvider, PayoutRef, PayoutUnavailable
from app.pii import ACCOUNT_NUMBER, PiiCipher
from app.risk import (
    CONFIRMATION_REQUIRED,
    DEPOSIT_DECLINED,
    Band,
    DepositFacts,
    PastDeposit,
    RiskAssessor,
    RiskOverride,
    decide,
    fingerprint,
)
from app.shapid import Directory, ShapIdError, demo_directory, resolve
from app.switch import AlreadyRedeemed, VoucherNotFound, VoucherSwitch

# How many of a user's past deposits risk reads at most.
RISK_HISTORY_LIMIT = 500

logger = logging.getLogger("app.deposits")

# One advisory lock per deposit, per schema (tests and the app share a database).
_ADVANCE_KEY = "hashtext('kasideposit-advance:' || current_schema() || ':' || :key)"
ADVANCE_TRY_LOCK_SQL = text(f"SELECT pg_try_advisory_lock({_ADVANCE_KEY})")
ADVANCE_UNLOCK_SQL = text(f"SELECT pg_advisory_unlock({_ADVANCE_KEY})")

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

# What `advance` still has work to do on.
IN_FLIGHT: frozenset[DepositStatus] = frozenset({S.CHARGED, S.PAYING})

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


def _log_transition(
    deposit_id: uuid.UUID, before: DepositStatus, after: DepositStatus, reason: str | None = None
) -> None:
    """Ids and states only (the logger also carries the personal-number filter)."""
    logger.info(
        "deposit %s: %s -> %s%s", deposit_id, before.value, after.value, f" ({reason})" if reason else ""
    )


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


@dataclass(frozen=True)
class DepositRecordView:
    """One of a user's deposits for their history: what was sent and where."""

    id: uuid.UUID
    reference: str
    status: str
    payout_cents: Cents
    value_cents: Cents
    fee_cents: Cents
    failure_reason: str | None
    created_at: datetime
    destination: dict[str, Any]  # public fields only: never an account number


def public_destination(stored: Mapping[str, Any]) -> dict[str, Any]:
    """What history may show of a stored destination. The account number
    stays encrypted; the app shows an account by its last four digits."""
    if stored.get("kind") == "account":
        return {
            "kind": "account",
            "name": stored.get("name"),
            "account_last4": stored.get("account_last4"),
            "bank_id": stored.get("bank_id"),
        }
    return {
        "kind": "shap_id",
        "shap_id": stored.get("shap_id"),
        "shap_name": stored.get("shap_name"),
        "bank_id": stored.get("bank_id"),
    }


def _is_unique_violation(exc: IntegrityError, constraint: str) -> bool:
    return isinstance(exc.orig, UniqueViolation) and exc.orig.diag.constraint_name == constraint


class DepositService:
    def __init__(
        self,
        switch: VoucherSwitch,
        provider: PayoutProvider,
        fee_basis_points: int,
        min_voucher_cents: Cents,
        token_ttl: timedelta,
        pii: PiiCipher | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        risk: RiskAssessor | None = None,
        risk_override: RiskOverride | None = None,
        risk_history_days: int = 30,
    ) -> None:
        self.risk = risk  # None: no assessment (unit tests that build the service directly)
        self.risk_override = risk_override
        self.risk_history_days = risk_history_days
        self.switch = switch
        self.provider = provider
        self.fee_basis_points = fee_basis_points
        self.min_voucher_cents = min_voucher_cents
        self.token_ttl = token_ttl
        self.pii = pii
        self.now = now

    # --- rules -------------------------------------------------------------

    def fee_for(self, amount_cents: Cents) -> Cents:
        """Our fee on a voucher: FEE_BASIS_POINTS of its amount, to the nearest cent."""
        return percent_of(amount_cents, self.fee_basis_points)

    def is_too_small(self, amount_cents: Cents) -> bool:
        """Below the minimum, or nothing left after the fee. Unreachable while
        vending enforces MIN_VOUCHER_CENTS and the fee is under 100%; kept so a
        change to either surfaces it rather than paying out nothing."""
        return amount_cents < self.min_voucher_cents or amount_cents <= self.fee_for(amount_cents)

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
            fee_cents=self.fee_for(info.amount_cents),
            payout_cents=max(info.amount_cents - self.fee_for(info.amount_cents), 0),
        )

    # --- create ------------------------------------------------------------

    def create_deposit(
        self,
        session: Session,
        voucher_token: str,
        destination: Mapping[str, Any],
        idempotency_key: str,
        user_id: uuid.UUID,
        risk_acknowledged: bool = False,
    ) -> tuple[DepositView, bool]:
        """Returns the deposit and whether it was created (False: a replay).
        `destination` is resolved here (the older request shape); a saved
        payout method arrives already resolved, through `create_deposit_to`."""
        stored_destination = self._resolve_destination(destination, demo_directory(session))
        session.rollback()  # the lookup's read; the deposit gets its own transaction
        return self.create_deposit_to(
            session,
            voucher_token,
            stored_destination,
            idempotency_key,
            user_id,
            destination_added_at=None,  # resolved just now: as new as a destination gets
            risk_acknowledged=risk_acknowledged,
        )

    def create_deposit_to(
        self,
        session: Session,
        voucher_token: str,
        stored_destination: dict[str, Any],
        idempotency_key: str,
        user_id: uuid.UUID,
        destination_added_at: datetime | None = None,
        risk_acknowledged: bool = False,
    ) -> tuple[DepositView, bool]:
        """Charge and record a deposit to an already-resolved destination.
        `destination_added_at` is when the saved payout method was added
        (None: resolved on the spot)."""
        self._screen(
            session,
            voucher_token,
            stored_destination,
            destination_added_at,
            idempotency_key,
            user_id,
            risk_acknowledged,
        )
        try:
            deposit = self._charge_and_record(
                session, voucher_token, stored_destination, idempotency_key, user_id
            )
            session.commit()
        except _Replay:
            return self._replay(session, idempotency_key, user_id), False
        except IntegrityError as exc:
            if not _is_unique_violation(exc, "uq_deposits_idempotency_key"):
                raise
            return self._replay(session, idempotency_key, user_id), False
        except Refused:
            session.rollback()
            raise

        self.advance(session, deposit.id)
        return DepositView.of(self._load(session, deposit.id)), True

    # --- risk --------------------------------------------------------------

    def _screen(
        self,
        session: Session,
        voucher_token: str,
        destination: Mapping[str, Any],
        destination_added_at: datetime | None,
        idempotency_key: str,
        user_id: uuid.UUID,
        risk_acknowledged: bool,
    ) -> None:
        """Refuse before charging if risk says so. Runs before any lock is
        taken, and its read transaction is ended before the decision is asked
        for. `decide` FAILS OPEN: an error here lets the deposit through."""
        if self.risk is None:
            return

        def gather() -> DepositFacts | None:
            try:
                return self._risk_facts(
                    session, voucher_token, destination, destination_added_at, idempotency_key, user_id
                )
            finally:
                session.rollback()  # never carry this read into the decision or the charge

        decision = decide(self.risk, self.risk_override, gather)
        if decision is None or decision.band is Band.ALLOW:
            return
        if decision.band is Band.REVIEW:
            raise Refused(DEPOSIT_DECLINED)
        if not risk_acknowledged:
            raise Refused(CONFIRMATION_REQUIRED)

    def _risk_facts(
        self,
        session: Session,
        voucher_token: str,
        destination: Mapping[str, Any],
        destination_added_at: datetime | None,
        idempotency_key: str,
        user_id: uuid.UUID,
    ) -> DepositFacts | None:
        """The facts for one deposit, from rows we already hold. None when
        there is nothing to assess: a replay is answered as before, and a
        token the charge would refuse anyway is left to refuse there."""
        now = self.now()
        if session.scalar(select(Deposit.id).where(Deposit.idempotency_key == idempotency_key)) is not None:
            return None
        token = session.get(VoucherToken, voucher_token)
        if token is None or token.consumed_at is not None or token.expires_at <= now:
            return None
        try:
            info = self.switch.lookup(session, token.voucher_pin)
        except VoucherNotFound:  # its message is the PIN: never let it reach a log
            return None
        if info.status is not VoucherStatus.ACTIVE:
            return None
        rows = session.execute(
            select(Deposit.amount_cents, Deposit.created_at, Deposit.destination)
            .where(
                Deposit.user_id == user_id,
                Deposit.status != S.FAILED,  # a failed payout handed the voucher back
                Deposit.created_at > now - timedelta(days=self.risk_history_days),
            )
            .order_by(Deposit.created_at.desc())
            .limit(RISK_HISTORY_LIMIT)
        ).all()
        return DepositFacts(
            user_id=user_id,
            now=now,
            amount_cents=info.amount_cents,
            voucher_issued_at=info.issued_at,
            destination=fingerprint(destination),
            destination_added_at=destination_added_at or now,
            history=tuple(PastDeposit(r.amount_cents, r.created_at, fingerprint(r.destination)) for r in rows),
        )

    def _replay(self, session: Session, idempotency_key: str, user_id: uuid.UUID) -> DepositView:
        session.rollback()
        existing = session.scalar(
            select(Deposit)
            .where(Deposit.idempotency_key == idempotency_key)
            .execution_options(populate_existing=True)
        )
        if existing is None:
            raise Refused("voucher_already_redeemed")
        if existing.user_id != user_id:
            session.rollback()
            raise Refused("deposit_not_found")
        return DepositView.of(existing)

    def _charge_and_record(
        self,
        session: Session,
        voucher_token: str,
        destination: dict[str, Any],
        idempotency_key: str,
        user_id: uuid.UUID,
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
        payout_cents = info.amount_cents - self.fee_for(info.amount_cents)
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
            payout_cents=charged.amount_cents - self.fee_for(charged.amount_cents),
            destination=destination,
            idempotency_key=idempotency_key,
            user_id=user_id,
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
        user_id: uuid.UUID,
    ) -> Deposit:
        """Insert as `pending`. A reference collision retries with a fresh one
        inside a savepoint; any other violation (the idempotency key) propagates."""
        for _ in range(MAX_REFERENCE_ATTEMPTS):
            deposit = Deposit(
                reference=new_reference(),
                voucher_pin=voucher_pin,
                amount_cents=amount_cents,
                fee_cents=amount_cents - payout_cents,
                payout_cents=payout_cents,
                destination=destination,
                status=S.PENDING,
                idempotency_key=idempotency_key,
                user_id=user_id,
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

    def _resolve_destination(
        self, destination: Mapping[str, Any], directory: Directory
    ) -> dict[str, Any]:
        """Validate and resolve before anything is charged. Returns what is stored."""
        kind = destination.get("kind")
        if kind == "shap_id":
            shap_id = destination.get("shap_id")
            if not isinstance(shap_id, str):
                raise Refused("shapid_invalid_format")
            try:
                resolved = resolve(shap_id, directory)
            except ShapIdError as exc:
                raise Refused(exc.reason) from None
            # shap_name is the scheme's masked display text, kept for history.
            return {
                "kind": "shap_id",
                "shap_id": shap_id,
                "shap_name": resolved.shap_name,
                "bank_id": resolved.bank_id,
            }
        if kind == "account":
            # POPIA: the number is stored encrypted, never in plaintext (app/pii.py).
            if self.pii is None:
                raise Refused("invalid_destination")
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
            sealed = self.pii.encrypt(number, ACCOUNT_NUMBER)
            return {
                "kind": "account",
                "name": name,
                "bank_id": bank,
                "account_last4": number[-4:],
                "account_number_encrypted": base64.b64encode(sealed).decode(),
            }
        raise Refused("invalid_destination")

    # --- payout ------------------------------------------------------------

    def _payout_destination(self, stored: Mapping[str, Any]) -> dict[str, Any]:
        """The destination as the payout provider needs it: an account number
        decrypted here, in memory, for this one call."""
        if stored.get("kind") != "account":
            return dict(stored)
        if self.pii is None:
            raise PayoutUnavailable("PII_KEY is not set: cannot decrypt the account number")
        sealed = base64.b64decode(stored["account_number_encrypted"])
        return {
            "kind": "account",
            "name": stored["name"],
            "bank_id": stored["bank_id"],
            "account_number": self.pii.decrypt(sealed, ACCOUNT_NUMBER),
        }

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

    # --- advancing a deposit -------------------------------------------------
    #
    # `advance` is the ONE way an in-flight deposit moves forward. The status
    # read (`get_deposit`), the create request (right after its commit) and the
    # background worker (app/worker.py) all call it; there is no other
    # implementation of the state machine.
    #
    # Each step has three phases, and no transaction is open in the middle one:
    #   1. read what is needed (no lock), then end the transaction;
    #   2. call the provider, OUTSIDE any transaction, so a slow provider can
    #      never trip the 10s idle-in-transaction limit or hold a row lock;
    #   3. lock the deposit row, RE-CHECK its status, and write only if it is
    #      still in the state phase 1 saw. Someone else may have moved it.
    #
    # Why a crash or a race cannot pay twice or post twice:
    # - Instructing: `create` always carries idempotency_key = the deposit id.
    #   The provider returns the same payout for the same key (the contract in
    #   app/payouts.py; a real provider keeps keys durably, the mock in memory).
    #   Killed after `create` but before phase 3 commits, the deposit is still
    #   `charged` with no payout row; the next advance calls `create` again with
    #   the same key and gets the SAME payout back. Two advances racing both get
    #   that one payout; phase 3's re-check (and the payouts primary key on
    #   deposit_id) lets only one of them record it.
    # - Settling or failing: `status` is a read. Killed after it but before
    #   phase 3 commits, nothing was written, and the next advance asks again.
    #   The ledger entries and the status change commit in ONE transaction, and
    #   phase 3 posts only while the locked row still says `paying`, so a second
    #   advance finds `settled` (or `failed`) and does nothing. Even past that
    #   re-check, `transition` refuses settled -> settled, which rolls back the
    #   entries with it: two guards, either of which alone prevents a double post.
    # - Across processes, `_advance_lock` (a Postgres advisory lock per deposit)
    #   keeps two instances from working on one deposit at all. It is a
    #   courtesy on top of the above, not what correctness rests on.
    #
    # Lock order (README, Conventions): float lock, then voucher row, then
    # deposit row. Settling never takes the float lock: it moves money from
    # user_payable to settlement and leaves the available float unchanged, and
    # a reversal only gives float back, so neither can overdraw it. Failing
    # takes the voucher row (`VoucherSwitch.hold`) BEFORE the deposit row.

    def advance(self, session: Session, deposit_id: uuid.UUID) -> bool:
        """Move an in-flight deposit forward as far as the provider allows.
        False if another process is advancing it right now (nothing done)."""
        with self._advance_lock(session, deposit_id) as held:
            if not held:
                return False
            self._instruct_payout(session, deposit_id)
            self._advance_payout(session, deposit_id)
            return True

    @contextmanager
    def _advance_lock(self, session: Session, deposit_id: uuid.UUID) -> Iterator[bool]:
        """A session-level advisory lock on its own autocommit connection, so it
        spans the provider call without holding a transaction open. Never waits:
        if someone else holds it, they are already doing this work."""
        params = {"key": str(deposit_id)}
        connection = session.get_bind().engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            held = bool(connection.scalar(ADVANCE_TRY_LOCK_SQL, params))
        except Exception:
            connection.close()
            raise
        try:
            yield held
        finally:
            if held:
                try:
                    connection.execute(ADVANCE_UNLOCK_SQL, params)
                except Exception:
                    # A pooled connection that may still hold the lock must never be reused.
                    connection.invalidate()
            connection.close()

    def _instruct_payout(self, session: Session, deposit_id: uuid.UUID) -> None:
        """`charged` -> `paying`: instruct the payout. If the provider cannot be
        reached, the deposit stays `charged` for next time."""
        # 1. Read.
        deposit = self._find(session, deposit_id)
        if deposit is None or deposit.status is not S.CHARGED:
            session.rollback()
            return
        amount_cents, stored = deposit.payout_cents, dict(deposit.destination)
        session.rollback()

        # 2. The provider, with no transaction open. Same key every time: see above.
        try:
            ref = self.provider.create(
                amount_cents, self._payout_destination(stored), idempotency_key=str(deposit_id)
            )
        except PayoutUnavailable:
            return

        # 3. Record it, if nobody has since.
        deposit = self._lock(session, deposit_id)
        if deposit is None or deposit.status is not S.CHARGED:
            session.rollback()  # already recorded: the same key gave them the same payout
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
        _log_transition(deposit_id, S.CHARGED, S.PAYING)

    def _advance_payout(self, session: Session, deposit_id: uuid.UUID) -> None:
        """`paying` -> `settled` or `failed`, once the provider knows."""
        # 1. Read.
        deposit = self._find(session, deposit_id)
        payout = session.get(Payout, deposit_id, populate_existing=True) if deposit else None
        if deposit is None or deposit.status is not S.PAYING or payout is None:
            session.rollback()  # paying is only ever reached with a payout row
            return
        provider_ref, voucher_pin = payout.provider_ref, deposit.voucher_pin
        session.rollback()

        # 2. The provider, with no transaction open. A read: asking twice is harmless.
        try:
            result = self.provider.status(PayoutRef(provider_ref))
        except PayoutUnavailable:
            return

        # 3. Write, if it is still paying.
        if result.status is PayoutStatus.FAILED:
            self.switch.hold(session, voucher_pin)  # voucher row before deposit row
        deposit = self._lock(session, deposit_id)
        payout = session.get(Payout, deposit_id, populate_existing=True)
        if deposit is None or deposit.status is not S.PAYING or payout is None:
            session.rollback()  # someone else settled or failed it
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
            session.commit()
            _log_transition(deposit_id, S.PAYING, S.SETTLED)
            return

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
        _log_transition(deposit_id, S.PAYING, S.FAILED, reason)

    def _find(self, session: Session, deposit_id: uuid.UUID) -> Deposit | None:
        return session.scalar(
            select(Deposit).where(Deposit.id == deposit_id).execution_options(populate_existing=True)
        )

    # --- status ------------------------------------------------------------

    def get_deposit(
        self, session: Session, deposit_id: uuid.UUID, user_id: uuid.UUID
    ) -> DepositView:
        deposit = self._load(session, deposit_id)
        if deposit.user_id != user_id:
            session.rollback()
            raise Refused("deposit_not_found")
        if deposit.status in IN_FLIGHT:
            session.rollback()
            self.advance(session, deposit_id)
            deposit = self._load(session, deposit_id)
        view = DepositView.of(deposit)
        session.rollback()  # end the read transaction; never leave it idle
        return view

    # --- history -----------------------------------------------------------

    def list_deposits(
        self, session: Session, user_id: uuid.UUID, limit: int
    ) -> list[DepositRecordView]:
        """The user's most recent deposits, newest first. Read-only."""
        rows = session.scalars(
            select(Deposit)
            .where(Deposit.user_id == user_id)
            .order_by(Deposit.created_at.desc(), Deposit.id)
            .limit(limit)
        ).all()
        records = [
            DepositRecordView(
                id=d.id,
                reference=d.reference,
                status=APP_STATUS[d.status],
                payout_cents=d.payout_cents,
                value_cents=d.amount_cents,
                fee_cents=d.fee_cents,
                failure_reason=d.failure_reason,
                created_at=d.created_at,
                destination=public_destination(d.destination),
            )
            for d in rows
        ]
        session.rollback()  # end the read transaction; never leave it idle
        return records
