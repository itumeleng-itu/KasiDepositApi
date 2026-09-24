"""Alembic environment.

Migrates the schema named by ENVIRONMENT: TEST_SCHEMA under test, DEV_SCHEMA
in development and production (see `Settings.active_schema`). The test suite
passes its own Settings in `config.attributes["settings"]`; otherwise they
come from the environment. alembic.ini never holds a URL.

`alembic_version` lives in the migrated schema (`version_table_schema`), so
each schema keeps its own migration history. The connection comes from
`make_engine`, so migrations get the same search_path and timeouts as the app.
"""

from logging.config import fileConfig

from alembic import context

from app.config import Settings, get_settings
from app.db import make_engine, session_setup_sql
from app.models import Base

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _settings() -> Settings:
    settings = config.attributes.get("settings")
    return settings if isinstance(settings, Settings) else get_settings()


def run_migrations_offline() -> None:
    settings = _settings()
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        version_table_schema=settings.active_schema,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.execute(session_setup_sql(settings.active_schema))
        context.run_migrations()


def run_migrations_online() -> None:
    settings = _settings()
    engine = make_engine(settings.database_url, settings.active_schema)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                version_table_schema=settings.active_schema,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
