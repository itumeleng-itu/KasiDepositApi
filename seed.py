"""Seed the development database with demo vouchers.

    python seed.py --count 20
    python seed.py --reset            # shows what would be deleted, deletes nothing
    python seed.py --reset --yes --count 20

Refuses to run unless ENVIRONMENT=development. Vouchers are vended through
VoucherSwitch, exactly as the demo till does; full PINs are printed so they
can be redeemed in the demo.
"""

import argparse
import secrets
import sys
from collections.abc import Sequence
from typing import TextIO

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.models import Voucher
from app.money import format_rand, rand_to_cents
from app.switch import VendedVoucher, VoucherSwitch

# Realistic spaza denominations.
DEMO_AMOUNTS_RAND = (50, 100, 200, 500, 1000)
MAX_COUNT = 1000


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vend demo vouchers into the development database.")
    parser.add_argument("--count", type=int, default=20, help=f"vouchers to vend (1-{MAX_COUNT}, default 20)")
    parser.add_argument("--reset", action="store_true", help="delete all vouchers first (needs --yes)")
    parser.add_argument("--yes", action="store_true", help="confirm --reset")
    args = parser.parse_args(argv)
    if not 1 <= args.count <= MAX_COUNT:
        parser.error(f"--count must be between 1 and {MAX_COUNT}")
    return args


def run(
    args: argparse.Namespace,
    settings: Settings,
    session_factory: sessionmaker[Session],
    out: TextIO,
) -> int:
    if settings.environment != "development":
        print(
            f"Refusing to seed: ENVIRONMENT is {settings.environment!r}, not 'development'.",
            file=out,
        )
        return 1

    switch = VoucherSwitch(settings.min_voucher_cents, settings.max_voucher_cents)
    amounts = [rand_to_cents(r) for r in DEMO_AMOUNTS_RAND]

    with session_factory() as session:
        if args.reset:
            existing = session.scalar(select(func.count()).select_from(Voucher)) or 0
            if not args.yes:
                print(
                    f"--reset would delete all {existing} voucher(s) from the development "
                    "database. Nothing was deleted. Re-run with --reset --yes to do it.",
                    file=out,
                )
                return 1
            # No CASCADE: once later phases reference vouchers, a reset that
            # would take ledger rows with it must fail rather than proceed.
            session.execute(text(f"TRUNCATE {Voucher.__tablename__}"))
            print(f"Deleted {existing} voucher(s).", file=out)

        vended = [switch.vend(session, secrets.choice(amounts)) for _ in range(args.count)]
        session.commit()

    _print_table(vended, out)
    return 0


def _print_table(vended: Sequence[VendedVoucher], out: TextIO) -> None:
    print(f"\n{'PIN':<18}{'AMOUNT':>12}  STATUS", file=out)
    for v in vended:
        print(f"{v.pin:<18}{format_rand(v.amount_cents):>12}  active", file=out)
    total = sum(v.amount_cents for v in vended)
    print(f"\n{len(vended)} voucher(s), {format_rand(total)} in total.", file=out)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    engine = make_engine(settings.database_url, settings.active_schema)
    try:
        return run(args, settings, make_session_factory(engine), sys.stdout)
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
