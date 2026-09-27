"""ShapID resolution: a mock of PayShap's proxy directory.

PayShap works in two phases. First the ShapID (a cellphone number, optionally
qualified with a bank as `+27821234567@fnb`) is resolved in the proxy directory,
before any clearing message exists. Only then does money move. So a
resolution failure means nothing was charged and nothing needs reversing, and
the deposit flow resolves the destination before it charges the voucher.

First the DEMO DIRECTORY (`demo_shapids`, filled from the till page): a
number listed there resolves to its name and bank, so a presenter's own
number shows their own name. With an @bank suffix for a different bank, it is
not found there.

Then the RESERVED TEST NUMBERS, so a presenter can show each failure on
purpose (the same numbers as the mobile app's fake, src/api/fake.ts, and its
TESTING.md):

    082 000 0009  shapid_not_found (not set up for PayShap)
    082 000 0008  shapid_suspended
    082 000 0007  shapid_ambiguous, unless an @bank suffix is present: then it
                  resolves at that bank
    082 000 0005  registered to someone else (MASKED_NAME), so adding it is
                  refused as shapid_name_mismatch

EVERY OTHER valid number is registered for PayShap, to whoever is asking
(`owner_names`, the user adding it) under their masked name, so a real
person's own number is accepted and a demo never fails by accident. Without
an owner (the public GET /v1/shapid) it shows MASKED_NAME. It is at the @bank
suffix's bank, or Capitec. With no real PayShap connection there is nothing
truer to say: the demo cannot know which numbers are really registered.

A ShapID that is not the canonical form the app sends (`+27`, then a mobile
number starting 6, 7 or 8, optionally `@<bank>`) is shapid_invalid_format.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.banks import BankId, parse_bank_id
from app.models import DemoShapId
from app.schemas.demo_shapid import mask_name

_SHAP_ID = re.compile(r"^(?P<number>\+27[678]\d{8})(?:@(?P<bank>[A-Za-z_]+))?$")

# POPIA: the scheme returns a MASKED name, an initial and surname, never a
# full legal name. It is display text only: show it back to the user so they
# can confirm the account is theirs, and never expand, split or store it as
# identity data.
MASKED_NAME = "M. Mothiba"
DEFAULT_BANK: BankId = "capitec"


@dataclass(frozen=True)
class ResolvedShapId:
    shap_id: str
    shap_name: str  # masked by the scheme; see the POPIA note above
    bank_id: BankId


class ShapIdError(Exception):
    """Resolution failed. `reason` is the exact string the app expects."""

    reason: str = ""


class ShapIdInvalidFormat(ShapIdError):
    reason = "shapid_invalid_format"


class ShapIdNotFound(ShapIdError):
    reason = "shapid_not_found"


class ShapIdSuspended(ShapIdError):
    reason = "shapid_suspended"


class ShapIdAmbiguous(ShapIdError):
    reason = "shapid_ambiguous"


# number (E.164, no suffix) -> (masked name, bank), or None if not listed.
Directory = Callable[[str], tuple[str, BankId] | None]


def demo_directory(session: Session) -> Directory:
    """The demo directory, read through `session`."""

    def lookup(number: str) -> tuple[str, BankId] | None:
        entry = session.get(DemoShapId, number)
        if entry is None:
            return None
        bank = parse_bank_id(entry.bank_id)
        return None if bank is None else (entry.shap_name, bank)

    return lookup


def _masked_or_default(owner_names: str | None) -> str:
    if owner_names:
        try:
            return mask_name(owner_names)
        except ValueError:
            pass  # not a first name and surname: the name check will refuse it
    return MASKED_NAME


# The reserved test numbers (E.164, no suffix). See the module docstring.
NOT_REGISTERED = "+27820000009"
SUSPENDED = "+27820000008"
AMBIGUOUS = "+27820000007"
SOMEONE_ELSES = "+27820000005"


def resolve(
    shap_id: str, directory: Directory | None = None, owner_names: str | None = None
) -> ResolvedShapId:
    """Resolve a ShapID or raise the ShapIdError for its scenario."""
    match = _SHAP_ID.fullmatch(shap_id)
    if match is None:
        raise ShapIdInvalidFormat(shap_id)
    suffix: BankId | None = None
    if match["bank"] is not None:
        # @suffix is case-insensitive, as in the app: @fnb, @FNB, @Fnb.
        suffix = parse_bank_id(match["bank"].lower())
        if suffix is None:
            raise ShapIdInvalidFormat(shap_id)

    listed = directory(match["number"]) if directory is not None else None
    if listed is not None:
        shap_name, bank = listed
        if suffix is not None and suffix != bank:
            raise ShapIdNotFound(shap_id)
        return ResolvedShapId(shap_id=shap_id, shap_name=shap_name, bank_id=bank)

    number = match["number"]
    if number == NOT_REGISTERED:
        raise ShapIdNotFound(shap_id)
    if number == SUSPENDED:
        raise ShapIdSuspended(shap_id)
    if number == AMBIGUOUS and suffix is None:
        raise ShapIdAmbiguous(shap_id)
    shap_name = MASKED_NAME if number == SOMEONE_ELSES else _masked_or_default(owner_names)
    return ResolvedShapId(shap_id=shap_id, shap_name=shap_name, bank_id=suffix or DEFAULT_BANK)

