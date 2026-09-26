"""Registering users and recognising them on later requests.

Registration (POST /v1/users) is who the user is: full names and SA ID
number. Where they are paid is added afterwards (app/payout_methods.py). In
this order, each refusal the app's exact reason:

1. The SA ID algorithm (app/sa_id.py): id_number_invalid, id_number_under_age.
2. Full names, as the app checks them: invalid_registration.
3. Identity verification (app/identity.py, a mock of Home Affairs).
4. One account per ID number, via its keyed hash:
   - same ID and same names: the same person on a new or reinstalled phone.
     Their old sessions are revoked and a new one issued; their payout
     methods are still theirs.
   - same ID, different names: id_number_already_registered.
   The unique constraint decides a race between two registrations of one ID.

Sessions: a registration returns a random bearer token. Only its SHA-256 is
stored, so a database leak yields no usable tokens. Every protected /v1 call
presents it; a missing, unknown or revoked token, or a suspended user, is
not_registered (401), and the app sends the user back to its register screen.

The ID number is never logged, never put in an exception, and never stored
in plaintext (app/pii.py).
"""

import hashlib
import secrets
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.deposits import Refused
from app.identity import IdentityRefused, IdentityVerifier
from app.models import User, UserSession, UserStatus
from app.pii import SA_ID, PiiCipher
from app.sa_id import InvalidSaId, parse_sa_id

FULL_NAMES_MAX = 100
# Polling calls GET /v1/deposits/{id} every second; recording each use would
# be a write per poll. Once a minute is plenty to see which sessions are live.
LAST_USED_RESOLUTION = timedelta(minutes=1)
_NAME_PUNCTUATION = frozenset(" '’-")


def normalise_names(raw: str) -> str:
    return " ".join(raw.split())


def valid_full_names(names: str) -> bool:
    """The app's validateFullNames: letters (accents allowed), spaces, hyphens
    and apostrophes; starts with a letter; at least two names."""
    if not 3 <= len(names) <= FULL_NAMES_MAX or not names[0].isalpha():
        return False
    for ch in names:
        if not (ch.isalpha() or unicodedata.category(ch).startswith("M") or ch in _NAME_PUNCTUATION):
            return False
    return sum(1 for part in names.split(" ") if any(c.isalpha() for c in part)) >= 2


def _same_person(user: User, full_names: str) -> bool:
    return user.full_names.casefold() == full_names.casefold()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Registered:
    user_id: uuid.UUID
    access_token: str
    full_names: str
    created: bool  # False: an existing user re-linked a phone


class UserService:
    def __init__(
        self,
        pii: PiiCipher | None,
        verifier: IdentityVerifier,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.pii = pii
        self.verifier = verifier
        self.now = now

    # --- registration ------------------------------------------------------

    def register(self, session: Session, full_names: str, id_number: str) -> Registered:
        if self.pii is None:
            # No key, no registration: never store an ID number unencrypted.
            raise Refused("registration_unavailable")
        try:
            parsed = parse_sa_id(id_number, self.now().date())
        except InvalidSaId as exc:
            raise Refused(exc.reason) from None
        names = normalise_names(full_names)
        if not valid_full_names(names):
            raise Refused("invalid_registration")
        try:
            verification_ref = self.verifier.verify(parsed.id_number, names)
        except IdentityRefused as exc:
            raise Refused(exc.reason) from None

        id_hash = self.pii.lookup_hash(parsed.id_number, SA_ID)
        existing = self._by_id_hash(session, id_hash)
        if existing is None:
            user = User(
                full_names=names,
                id_number_hash=id_hash,
                id_number_encrypted=self.pii.encrypt(parsed.id_number, SA_ID),
                date_of_birth=parsed.date_of_birth,
                status=UserStatus.ACTIVE,
                verification_ref=verification_ref,
                verified_at=self.now(),
            )
            try:
                with session.begin_nested():
                    session.add(user)
            except IntegrityError:
                # Another registration of this ID won the race: treat as a return.
                existing = self._by_id_hash(session, id_hash)
                if existing is None:
                    raise
            else:
                token = self._new_session(session, user.id)
                session.commit()
                return Registered(user.id, token, user.full_names, created=True)

        if existing.status is not UserStatus.ACTIVE or not _same_person(existing, names):
            session.rollback()
            raise Refused("id_number_already_registered")
        session.execute(
            update(UserSession)
            .where(UserSession.user_id == existing.id, UserSession.revoked_at.is_(None))
            .values(revoked_at=self.now())
        )
        token = self._new_session(session, existing.id)
        session.commit()
        return Registered(existing.id, token, existing.full_names, created=False)

    def _by_id_hash(self, session: Session, id_hash: str) -> User | None:
        return session.scalar(select(User).where(User.id_number_hash == id_hash))

    def _new_session(self, session: Session, user_id: uuid.UUID) -> str:
        token = secrets.token_urlsafe(32)
        session.add(UserSession(user_id=user_id, token_hash=token_hash(token)))
        session.flush()
        return token

    # --- authentication ----------------------------------------------------

    def authenticate(self, session: Session, authorization: str | None) -> uuid.UUID:
        """The user id behind `Authorization: Bearer <token>`, or not_registered."""
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise Refused("not_registered")
        row = session.execute(
            select(UserSession, User.status)
            .join(User, User.id == UserSession.user_id)
            .where(UserSession.token_hash == token_hash(token.strip()))
        ).first()
        if row is None:
            session.rollback()
            raise Refused("not_registered")
        user_session, status = row
        user_id = user_session.user_id  # read before any rollback expires the row
        if user_session.revoked_at is not None or status is not UserStatus.ACTIVE:
            session.rollback()
            raise Refused("not_registered")
        now = self.now()
        if user_session.last_used_at is None or now - user_session.last_used_at >= LAST_USED_RESOLUTION:
            user_session.last_used_at = now
            session.commit()
        else:
            session.rollback()  # end the read transaction; never leave it idle
        return user_id
