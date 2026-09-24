"""Banks, as the mobile app names them.

Internally a bank is the app's lowercase slug (`BankId` in the app's
src/domain/banks.ts). On the wire it is the upper-case code in the app's
`BANK_API_CODES` (src/api/wire.ts). Both lists must match the app exactly.
"""

from typing import Literal, cast, get_args

BankId = Literal[
    "capitec",
    "fnb",
    "standard_bank",
    "absa",
    "nedbank",
    "tymebank",
    "african_bank",
    "discovery_bank",
    "bank_zero",
    "investec",
]

BANK_IDS: frozenset[str] = frozenset(get_args(BankId))

BANK_API_CODES: dict[str, str] = {bank: bank.upper() for bank in BANK_IDS}
"""slug -> wire code, e.g. "standard_bank" -> "STANDARD_BANK"."""

BANK_IDS_BY_API_CODE: dict[str, str] = {code: bank for bank, code in BANK_API_CODES.items()}


def parse_bank_id(value: str) -> BankId | None:
    """The slug as a BankId, or None if it is not one of the app's banks."""
    return cast(BankId, value) if value in BANK_IDS else None
