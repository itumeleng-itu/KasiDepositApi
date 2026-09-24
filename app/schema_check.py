"""Refuse to serve on a database schema that is not at the latest migration.

Without this, an unmigrated schema starts "healthy" and then answers every
voucher and deposit request with a 500. Checked once, at startup: a schema
behind the code stops the process with a clear message instead.

A database that cannot be reached at startup does not stop the process; it
may come back, and /health reports it as unreachable meanwhile.
"""

import logging
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError

log = logging.getLogger(__name__)

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


class SchemaNotCurrent(RuntimeError):
    pass


def expected_revision() -> str:
    head = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()
    if head is None:
        raise SchemaNotCurrent("no migrations found")
    return head


def check_schema_is_current(engine: Engine) -> None:
    expected = expected_revision()
    try:
        with engine.connect() as conn:
            try:
                current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            except ProgrammingError:  # no alembic_version table: never migrated
                current = None
    except OperationalError:
        log.warning("database unreachable at startup; schema version not checked")
        return
    if current != expected:
        raise SchemaNotCurrent(
            f"database schema is at {current or 'no revision'}, the code needs {expected}: "
            "run `python -m alembic upgrade head` first"
        )
