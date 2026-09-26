"""Does a PayShap number's masked name belong to this user?

The directory returns a MASKED name (POPIA): an initial and a surname, such as
"T. Mokoena". It is compared with the user's registered full names here, once,
when a PayShap number is added; only the pass/fail outcome is kept.

A match needs both:
- the surname equals the user's last name, and
- the initial is the first letter of one of the user's other names, so a
  parent's number ("M. Mokoena") is not accepted for "Thabo Mokoena".

Case, accents, spaces, hyphens and apostrophes are ignored ("O'Neill-Botha"
matches "ONeill Botha"). A masked name in any other shape (asterisks, no
initial) does not match: better to refuse and offer a bank account than to
pay a stranger.
"""

import re
import unicodedata

_MASKED = re.compile(r"^\s*(?P<initial>[^\W\d_])\s*\.?\s+(?P<surname>.+?)\s*$")


def _fold(text: str) -> str:
    """Lowercase, accents removed, letters only."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if ch.isalpha())


def masked_name_matches(masked: str, full_names: str) -> bool:
    match = _MASKED.fullmatch(masked)
    if match is None or "*" in masked:
        return False
    names = full_names.split()
    if len(names) < 2:
        return False
    surname_ok = _fold(match["surname"]) == _fold(names[-1])
    initial = _fold(match["initial"])
    initial_ok = any(_fold(name).startswith(initial) for name in names[:-1])
    return surname_ok and initial_ok
