"""Alembic environment configuration."""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# Import models for autogenerate
from app.core.database import Base
from app.models import *  # noqa: F401, F403
from app.config import settings

# Alembic Config object
config = context.config

# Override sqlalchemy.url with our config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Model MetaData for autogenerate
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Supports a pre-existing connection injected via ``config.attributes["connection"]``
    (used in tests to bypass the app DATABASE_URL and run against SQLite).
    """
    injected_connection = config.attributes.get("connection", None)

    if injected_connection is not None:
        # Test path: caller already has an open connection — use it directly.
        # render_as_batch=True is required for SQLite, which cannot ALTER columns
        # or constraints in-place; the batch strategy uses copy-and-move instead.
        context.configure(
            connection=injected_connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
