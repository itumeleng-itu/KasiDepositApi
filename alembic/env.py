"""Alembic environment.

The database URL and schema come from the Alembic config when a caller sets
them (the test suite does, to target its own schema), otherwise from app
settings (DATABASE_URL, schema `public`). alembic.ini never holds a URL.

Migrations run with `search_path` pinned to the target schema, and the
`alembic_version` table lives in that schema too, so each schema carries its
own independent migration history.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool, text

from app.config import get_settings, normalize_database_url, validate_schema_name
from app.models import Base

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url_and_schema() -> tuple[str, str]:
    override = config.get_main_option("sqlalchemy.url")
    if override:
        schema = config.get_main_option("kasideposit.schema") or "public"
        return normalize_database_url(override), validate_schema_name(schema)
    settings = get_settings()
    return settings.database_url, settings.database_schema


def run_migrations_offline() -> None:
    url, schema = _database_url_and_schema()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        version_table_schema=schema,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.execute(f'SET search_path TO "{schema}"')
        context.run_migrations()


def run_migrations_online() -> None:
    url, schema = _database_url_and_schema()
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        connection.execute(text(f'SET search_path TO "{schema}"'))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=schema,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
