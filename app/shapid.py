"""ShapID resolution: a mock of PayShap's proxy directory.

PayShap works in two phases. First the ShapID (a cellphone number, optionally
qualified with a bank as `+27821234567@fnb`) is resolved in the proxy directory,
before any clearing message exists. Only then does money move. So a
resolution failure means nothing was charged and nothing needs reversing, and
the deposit flow resolves the destination before it charges the voucher.

This mock matches the mobile app's fake (src/api/fake.ts in the app, and its
TESTING.md) scenario for scenario, keyed off the number's last digit:

    9     shapid_not_found
    8     shapid_suspended
    7     shapid_ambiguous, unless an @bank suffix is present: then it
          resolves at that bank
    else  "M. Mothiba" at Capitec

A ShapID that is not the canonical form the app sends (`+27`, then a mobile
number starting 6, 7 or 8, optionally `@<bank>`) is shapid_invalid_format.
"""

import re
from dataclasses import dataclass

from app.banks import BankId, parse_bank_id

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


def resolve(shap_id: str) -> ResolvedShapId:
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

    last_digit = match["number"][-1]
    if last_digit == "9":
        raise ShapIdNotFound(shap_id)
    if last_digit == "8":
        raise ShapIdSuspended(shap_id)
    if last_digit == "7":
        if suffix is None:
            raise ShapIdAmbiguous(shap_id)
        return ResolvedShapId(shap_id=shap_id, shap_name=MASKED_NAME, bank_id=suffix)
    return ResolvedShapId(shap_id=shap_id, shap_name=MASKED_NAME, bank_id=DEFAULT_BANK)

