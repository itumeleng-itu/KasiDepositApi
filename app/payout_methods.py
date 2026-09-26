"""Where a user can be paid: PayShap numbers and bank accounts.

Registration says who someone is; payout methods say where their money goes.
The user chooses which kind to add, and each is checked when it is added, so
a saved method is never just an unverified claim:

- PayShap number: resolved in the directory (app/shapid.py; the demo
  directory while there is no bank connection). Not registered for PayShap is
  the directory's own refusal (shapid_not_found, ...). Registered, but the
  masked name on it is not this user (app/names.py): shapid_name_mismatch.
  Either way the app offers "use a bank account instead".
- Bank account: verified to exist and to belong to the user's own ID number
  (app/account_verification.py). The holder is always the registered name;
  the user cannot type someone else's. The number is stored as ciphertext
  with a keyed hash (app/pii.py) and decrypted only to instruct a payout.

A user may keep up to MAX_METHODS. Exactly one is the default (the "Paying
into" line in the app) whenever there is at least one: the first one added
becomes it, and removing the default promotes the newest remaining. Adding a
method that is already saved returns it rather than a duplicate.

A deposit sends `payout_method_id`; `destination_for_deposit` turns it into
the snapshot stored on the deposit, so later edits never rewrite history.
"""

import base64
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.account_verification import AccountRefused, AccountVerifier
from app.banks import BANK_IDS_BY_API_CODE
from app.deposits import Refused
from app.models import PayoutMethod, PayoutMethodKind, User
from app.names import masked_name_matches
from app.pii import ACCOUNT_NUMBER, SA_ID, PiiCipher
from app.shapid import ShapIdError, demo_directory, resolve

MAX_METHODS = 5
# The app's rule (src/domain/account.ts): 7 to 11 digits.
_ACCOUNT_NUMBER = re.compile(r"^[0-9]{7,11}$")


@dataclass(frozen=True)
class PayoutMethodView:
    id: uuid.UUID
    kind: str
    bank_id: str
    is_default: bool
    shap_id: str | None
    shap_name: str | None
    account_holder: str | None
    account_last4: str | None

    @classmethod
    def of(cls, method: PayoutMethod) -> "PayoutMethodView":
        return cls(
            id=method.id,
            kind=method.kind.value,
            bank_id=method.bank_id,
            is_default=method.is_default,
            shap_id=method.shap_id,
            shap_name=method.shap_name,
            account_holder=method.account_holder,
            account_last4=method.account_last4,
        )


class PayoutMethodService:
    def __init__(
        self,
        pii: PiiCipher | None,
        account_verifier: AccountVerifier,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.pii = pii
        self.account_verifier = account_verifier
        self.now = now

    # --- reading -----------------------------------------------------------

    def list(self, session: Session, user_id: uuid.UUID) -> list[PayoutMethodView]:
        """Default first, then newest first."""
        rows = session.scalars(
            select(PayoutMethod)
            .where(PayoutMethod.user_id == user_id)
            .order_by(PayoutMethod.is_default.desc(), PayoutMethod.created_at.desc(), PayoutMethod.id)
        ).all()
        views = [PayoutMethodView.of(m) for m in rows]
        session.rollback()  # end the read transaction; never leave it idle
        return views

    # --- adding ------------------------------------------------------------

    def add_shap_id(
        self, session: Session, user_id: uuid.UUID, shap_id: str, make_default: bool
    ) -> PayoutMethodView:
        try:
            resolved = resolve(shap_id, demo_directory(session))
        except ShapIdError as exc:
            session.rollback()
            raise Refused(exc.reason) from None
        user = self._user(session, user_id)
        if not masked_name_matches(resolved.shap_name, user.full_names):
            session.rollback()
            raise Refused("shapid_name_mismatch")

        existing = session.scalar(
            select(PayoutMethod).where(PayoutMethod.user_id == user_id, PayoutMethod.shap_id == shap_id)
        )
        if existing is not None:
            return self._finish(session, user_id, existing, make_default)
        method = PayoutMethod(
            user_id=user_id,
            kind=PayoutMethodKind.SHAP_ID,
            bank_id=resolved.bank_id,
            shap_id=shap_id,
            shap_name=resolved.shap_name,
            verification_ref="shapid-directory",
            verified_at=self.now(),
        )
        return self._insert(session, user_id, method, make_default)

    def add_account(
        self,
        session: Session,
        user_id: uuid.UUID,
        bank_code: str,
        account_number: str,
        make_default: bool,
    ) -> PayoutMethodView:
        if self.pii is None:
            raise Refused("accounts_unavailable")  # never store an account number unencrypted
        bank_id = BANK_IDS_BY_API_CODE.get(bank_code)
        if bank_id is None or not _ACCOUNT_NUMBER.fullmatch(account_number):
            raise Refused("invalid_account")
        user = self._user(session, user_id)
        id_number = self.pii.decrypt(user.id_number_encrypted, SA_ID)
        try:
            verification_ref = self.account_verifier.verify(id_number, bank_id, account_number)
        except AccountRefused as exc:
            session.rollback()
            raise Refused(exc.reason) from None

        # Keyed on bank and number: the same digits at two banks are two accounts.
        account_hash = self.pii.lookup_hash(f"{bank_id}:{account_number}", ACCOUNT_NUMBER)
        existing = session.scalar(
            select(PayoutMethod).where(
                PayoutMethod.user_id == user_id, PayoutMethod.account_number_hash == account_hash
            )
        )
        if existing is not None:
            return self._finish(session, user_id, existing, make_default)
        method = PayoutMethod(
            user_id=user_id,
            kind=PayoutMethodKind.ACCOUNT,
            bank_id=bank_id,
            account_holder=user.full_names,
            account_last4=account_number[-4:],
            account_number_encrypted=self.pii.encrypt(account_number, ACCOUNT_NUMBER),
            account_number_hash=account_hash,
            verification_ref=verification_ref,
            verified_at=self.now(),
        )
        return self._insert(session, user_id, method, make_default)

    def _insert(
        self, session: Session, user_id: uuid.UUID, method: PayoutMethod, make_default: bool
    ) -> PayoutMethodView:
        count = session.scalar(
            select(func.count()).select_from(PayoutMethod).where(PayoutMethod.user_id == user_id)
        )
        if count and count >= MAX_METHODS:
            session.rollback()
            raise Refused("payout_method_limit")
        session.add(method)
        session.flush()
        return self._finish(session, user_id, method, make_default or not count)

    def _finish(
        self, session: Session, user_id: uuid.UUID, method: PayoutMethod, make_default: bool
    ) -> PayoutMethodView:
        if make_default and not method.is_default:
            self._make_default(session, user_id, method)
        session.commit()
        return PayoutMethodView.of(method)

    # --- choosing and removing ---------------------------------------------

    def set_default(self, session: Session, user_id: uuid.UUID, method_id: uuid.UUID) -> None:
        method = self._owned(session, user_id, method_id)
        if not method.is_default:
            self._make_default(session, user_id, method)
        session.commit()

    def remove(self, session: Session, user_id: uuid.UUID, method_id: uuid.UUID) -> None:
        method = self._owned(session, user_id, method_id)
        was_default = method.is_default
        session.delete(method)
        session.flush()
        if was_default:
            newest = session.scalar(
                select(PayoutMethod)
                .where(PayoutMethod.user_id == user_id)
                .order_by(PayoutMethod.created_at.desc(), PayoutMethod.id)
                .limit(1)
            )
            if newest is not None:
                newest.is_default = True
        session.commit()

    def _make_default(self, session: Session, user_id: uuid.UUID, method: PayoutMethod) -> None:
        # Clear the old default first: the partial unique index allows one at a time.
        session.execute(
            update(PayoutMethod)
            .where(PayoutMethod.user_id == user_id, PayoutMethod.is_default.is_(True))
            .values(is_default=False)
            .execution_options(synchronize_session="fetch")
        )
        session.flush()
        method.is_default = True
        session.flush()

    # --- deposits ----------------------------------------------------------

    def destination_for_deposit(
        self, session: Session, user_id: uuid.UUID, method_id: uuid.UUID
    ) -> dict[str, Any]:
        """The snapshot stored on a deposit. A PayShap number is resolved
        again first, so a number suspended since it was added is refused
        before anything is charged."""
        method = self._owned(session, user_id, method_id)
        if method.kind is PayoutMethodKind.SHAP_ID:
            assert method.shap_id is not None
            try:
                resolve(method.shap_id, demo_directory(session))
            except ShapIdError as exc:
                session.rollback()
                raise Refused(exc.reason) from None
            destination = {
                "kind": "shap_id",
                "shap_id": method.shap_id,
                "shap_name": method.shap_name,
                "bank_id": method.bank_id,
            }
        else:
            assert method.account_number_encrypted is not None
            destination = {
                "kind": "account",
                "name": method.account_holder,
                "bank_id": method.bank_id,
                "account_last4": method.account_last4,
                "account_number_encrypted": base64.b64encode(method.account_number_encrypted).decode(),
            }
        session.rollback()  # the deposit is created in its own transaction
        return destination

    # --- helpers -----------------------------------------------------------

    def _user(self, session: Session, user_id: uuid.UUID) -> User:
        user = session.get(User, user_id)
        if user is None:  # authenticated a moment ago; only a deleted user gets here
            session.rollback()
            raise Refused("not_registered")
        return user

    def _owned(self, session: Session, user_id: uuid.UUID, method_id: uuid.UUID) -> PayoutMethod:
        method = session.get(PayoutMethod, method_id)
        if method is None or method.user_id != user_id:
            session.rollback()
            raise Refused("payout_method_not_found")
        return method
