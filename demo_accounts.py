"""Create the demo accounts: one for the team's demo, one for the judges.

    python demo_accounts.py            # create them, or bring them back in line
    python demo_accounts.py --list     # print the details to type in, change nothing

Each account goes through the same checks as a real sign-up, in the same
order the app takes them, so it is validated rather than inserted:

1. Its PayShap number is listed in the demo PayShap directory under the
   account's masked name and bank (what the till's manager screen does), so
   the number "is on PayShap".
2. It is registered through UserService: the SA ID algorithm (13 digits, real
   birth date, citizenship digit, Luhn, 18 or older), the names and the
   identity mock. The ID is stored encrypted under PII_KEY, like any user.
3. The PayShap number is added through PayoutMethodService: resolved in the
   directory, and its masked name must match the account
   (shapid_name_mismatch otherwise). It becomes the default payout method.

Safe to run again: the same ID and names are the same person, and an existing
directory entry or payout method is reused. Running it signs the account out
of any phone it is signed in on (re-registering revokes old sessions); typing
the same names and ID on the register screen signs back in.

On the register screen, type the full names and ID number below exactly. The
backend recognises the account, and the app goes straight to "Enter your
voucher PIN", paying into its PayShap number.

The ID numbers and PayShap numbers are mock: valid, belonging to nobody, so
no real ID or phone number appears on screen in a demo, and no real person's
number is taken (a PayShap number can be saved by one user only: refused as
shapid_already_linked for anyone else). `seed.py --reset` deletes these
accounts along with every other user; run this again afterwards.

Refuses to run unless ENVIRONMENT=development, like seed.py. Needs PII_KEY,
the same value as the hosted API's, since both use one database.
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TextIO

from sqlalchemy.orm import Session, sessionmaker

from app.account_verification import MockAccountVerifier
from app.banks import BANK_API_CODES
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.deposits import Refused
from app.identity import MockIdentityVerifier
from app.models import DemoShapId
from app.payout_methods import PayoutMethodService
from app.pii import PiiCipher
from app.schemas.demo_shapid import mask_name, normalise_number
from app.users import UserService


@dataclass(frozen=True)
class DemoAccount:
    label: str
    full_names: str
    id_number: str  # mock: passes the SA ID checks, belongs to nobody
    payshap_number: str  # as people write it; normalised to +27... here
    bank: str  # bank slug, as in app/banks.py


ACCOUNTS: tuple[DemoAccount, ...] = (
    DemoAccount(
        label="Team (our demo)",
        full_names="Itumeleng Tjale",
        id_number="9505155800088",
        payshap_number="082 000 0003",
        bank="capitec",
    ),
    DemoAccount(
        label="Judges (testing)",
        full_names="Demo Judge",
        id_number="9103034800089",
        payshap_number="082 000 0002",
        bank="capitec",
    ),
)


@dataclass(frozen=True)
class Outcome:
    account: DemoAccount
    shap_id: str
    shap_name: str
    created: bool


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create the demo accounts in the development database.")
    parser.add_argument("--list", action="store_true", help="print the accounts' details, change nothing")
    return parser.parse_args(argv)


def ensure_account(
    session: Session, users: UserService, methods: PayoutMethodService, account: DemoAccount
) -> Outcome:
    """Raises Refused with the app's reason if any check fails."""
    shap_id = normalise_number(account.payshap_number)
    shap_name = mask_name(account.full_names)

    # 1. On PayShap: list the number in the demo directory.
    entry = session.get(DemoShapId, shap_id)
    if entry is None:
        session.add(DemoShapId(number=shap_id, shap_name=shap_name, bank_id=account.bank))
    else:
        entry.shap_name, entry.bank_id = shap_name, account.bank
    session.commit()

    # 2. Registered: ID checks, names, identity mock, one account per ID.
    registered = users.register(session, account.full_names, account.id_number)

    # 3. Paid by PayShap: resolved and name-matched, made the default.
    method = methods.add_shap_id(session, registered.user_id, shap_id, make_default=True)
    return Outcome(account, method.shap_id or shap_id, method.shap_name or shap_name, registered.created)


def run(
    args: argparse.Namespace,
    settings: Settings,
    session_factory: sessionmaker[Session],
    out: TextIO,
) -> int:
    if args.list:
        _print_details(out)
        return 0
    if settings.environment != "development":
        print(
            f"Refusing to create demo accounts: ENVIRONMENT is {settings.environment!r}, not 'development'.",
            file=out,
        )
        return 1
    if not settings.pii_key:
        print("PII_KEY is not set: ID numbers can't be stored. Use the hosted API's value.", file=out)
        return 1

    pii = PiiCipher(settings.pii_key)
    users = UserService(pii=pii, verifier=MockIdentityVerifier())
    methods = PayoutMethodService(pii=pii, account_verifier=MockAccountVerifier())
    outcomes: list[Outcome] = []
    with session_factory() as session:
        for account in ACCOUNTS:
            try:
                outcomes.append(ensure_account(session, users, methods, account))
            except Refused as exc:
                session.rollback()
                print(f"{account.label}: refused ({exc.reason}). Nothing more was done.", file=out)
                return 1

    for o in outcomes:
        state = "created" if o.created else "already existed, checked again"
        print(
            f"{o.account.label}: {state}. PayShap {o.shap_id} -> {o.shap_name} "
            f"({BANK_API_CODES[o.account.bank]}), default payout method.",
            file=out,
        )
    print("", file=out)
    _print_details(out)
    return 0


def _print_details(out: TextIO) -> None:
    print("Type these on the register screen:\n", file=out)
    print(f"{'ACCOUNT':<20}{'FULL NAMES':<20}{'ID NUMBER':<16}PAYSHAP NUMBER", file=out)
    for a in ACCOUNTS:
        bank = BANK_API_CODES[a.bank]
        print(f"{a.label:<20}{a.full_names:<20}{a.id_number:<16}{a.payshap_number} ({bank})", file=out)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list:
        _print_details(sys.stdout)
        return 0
    settings = get_settings()
    engine = make_engine(settings.database_url, settings.active_schema)
    try:
        return run(args, settings, make_session_factory(engine), sys.stdout)
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
