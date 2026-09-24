"""Engine and session plumbing.

Nothing is created at import time: `create_app` builds the engine from the
settings it is given and keeps it on `app.state`, so tests can point an app
at their own schema without touching development data.

Every connection, in every environment and in Alembic, is set up the same way
as it opens:

- `search_path` pinned to the configured schema, so tables and enum types
  resolve there and nowhere else;
- timeouts, so a bad network (the demo runs on venue wifi) fails fast and
  legibly instead of hanging: connect in 10s, statements in 30s, lock waits
  in 5s, and a transaction left idle is killed after 10s;
- TCP keepalives, because `statement_timeout` is enforced by the server and
  cannot fire if the network has dropped: keepalives let the client notice a
  dead connection and fail rather than wait forever.
"""

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

from app.config import validate_schema_name

CONNECT_TIMEOUT_SECONDS = 10
STATEMENT_TIMEOUT = "30s"
LOCK_TIMEOUT = "5s"
IDLE_IN_TRANSACTION_TIMEOUT = "10s"

CONNECT_ARGS: dict[str, int] = {
    "connect_timeout": CONNECT_TIMEOUT_SECONDS,
    "keepalives": 1,
    "keepalives_idle": 10,
    "keepalives_interval": 5,
    "keepalives_count": 3,
}


def session_setup_sql(schema: str) -> str:
    """The statements run on every new connection, as one round trip."""
    schema = validate_schema_name(schema)
    return (
        f'SET search_path TO "{schema}"; '
        f"SET statement_timeout = '{STATEMENT_TIMEOUT}'; "
        f"SET lock_timeout = '{LOCK_TIMEOUT}'; "
        f"SET idle_in_transaction_session_timeout = '{IDLE_IN_TRANSACTION_TIMEOUT}'"
    )


def make_engine(database_url: str, schema: str) -> Engine:
    setup_sql = session_setup_sql(schema)
    engine = create_engine(database_url, pool_pre_ping=True, connect_args=CONNECT_ARGS)

    @event.listens_for(engine, "connect")
    def _set_up_session(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(setup_sql)
        cursor.close()
        dbapi_connection.commit()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency: one session per request. Routes commit explicitly."""
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        yield session
