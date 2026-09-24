"""Money is integer cents. Everywhere.

No floats, no `Decimal` in the database, no arithmetic on formatted strings.
`format_rand` is for display only; `rand_to_cents` exists for the seed script
and is the one place a rand amount is ever parsed.
"""

from decimal import Decimal, InvalidOperation

Cents = int


def format_rand(cents: Cents) -> str:
    """123456 -> "R1 234.56". Space thousands separator, always two decimals."""
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise TypeError(f"cents must be an int, got {type(cents).__name__}")
    sign = "-" if cents < 0 else ""
    rand, remainder = divmod(abs(cents), 100)
    return f"{sign}R{rand:,}.{remainder:02d}".replace(",", " ")


def rand_to_cents(rand: str | int | float) -> Cents:
    """Convert a rand amount to cents. For the seed script only.

    Accepts 500, 12.5, "500", "1 000.00" or "R1 000.00". Amounts with more
    than two decimal places raise rather than round.
    """
    if isinstance(rand, bool):
        raise TypeError("rand must be a number or string, not bool")
    if isinstance(rand, int):
        return rand * 100
    if isinstance(rand, float):
        # str() gives the shortest repr, so 12.34 parses as "12.34", not 12.339999...
        text = str(rand)
    else:
        text = rand.strip().removeprefix("R").replace(" ", "")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"not a rand amount: {rand!r}") from None
    if not amount.is_finite():
        raise ValueError(f"not a rand amount: {rand!r}")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"{rand!r} has fractions of a cent")
    return int(cents)
