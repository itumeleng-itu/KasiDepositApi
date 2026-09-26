"""South African ID numbers: YYMMDD SSSS C A Z.

The same checks as the mobile app's src/domain/saId.ts, so an ID the app
accepts is accepted here and one it refuses is refused for the same reason:

    YYMMDD  date of birth (a year that would be in the future is the 1900s)
    SSSS    sequence: not checked
    C       0 citizen, 1 permanent resident, 2 refugee
    A       not checked (older IDs vary)
    Z       Luhn check digit over all 13 digits

Well formed is all this proves. It does not prove the number belongs to a
living person with these names; that needs Home Affairs (app/identity.py).
"""

import re
from dataclasses import dataclass
from datetime import date

MIN_AGE_YEARS = 18

_DIGITS = re.compile(r"^[0-9]{13}$")


@dataclass(frozen=True)
class ParsedSaId:
    id_number: str
    date_of_birth: date


class InvalidSaId(Exception):
    """`reason` is the app's refusal: id_number_invalid or id_number_under_age."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def has_valid_checksum(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _age_on(dob: date, today: date) -> int:
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def parse_sa_id(raw: str, today: date) -> ParsedSaId:
    """Raises InvalidSaId. Never echoes the number in the exception."""
    id_number = raw.strip()
    if not _DIGITS.fullmatch(id_number):
        raise InvalidSaId("id_number_invalid")

    yy, month, day = int(id_number[0:2]), int(id_number[2:4]), int(id_number[4:6])
    year = 2000 + yy
    if year > today.year:
        year -= 100
    try:
        dob = date(year, month, day)
        if dob > today:
            dob = date(year - 100, month, day)
    except ValueError:
        raise InvalidSaId("id_number_invalid") from None

    if id_number[10] not in "012":
        raise InvalidSaId("id_number_invalid")
    if not has_valid_checksum(id_number):
        raise InvalidSaId("id_number_invalid")
    if _age_on(dob, today) < MIN_AGE_YEARS:
        raise InvalidSaId("id_number_under_age")
    return ParsedSaId(id_number=id_number, date_of_birth=dob)
