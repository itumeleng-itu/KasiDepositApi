"""Engine and session plumbing.

Nothing is created at import time: `create_app` builds the engine from the
settings it is given and keeps it on `app.state`, so tests can point an app
at their own schema without touching development data.

Every connection's `search_path` is pinned to the configured schema, so
tables and enum types resolve there and nowhere else.
"""

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

from app.config import validate_schema_name

CONNECT_TIMEOUT_SECONDS = 5


def make_engine(database_url: str, schema: str = "public") -> Engine:
    schema = validate_schema_name(schema)
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )

    @event.listens_for(engine, "connect")
    def _set_search_path(
        dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry
    ) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
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


def same_database(url_a: str, url_b: str) -> bool:
    """True if two connection URLs address the same database.

    Compares user, host, port and database name rather than raw strings, so
    driver prefixes, passwords and query parameters don't hide a match. The
    user is part of the identity because pooled hosts (e.g. Supabase) route
    tenants by user name: two projects can share host, port and database name.
    """
    a, b = make_url(url_a), make_url(url_b)
    return (
        (a.username or "") == (b.username or "")
        and (a.host or "").lower() == (b.host or "").lower()
        and (a.port or 5432) == (b.port or 5432)
        and (a.database or "") == (b.database or "")
    )
