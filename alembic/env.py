"""Alembic environment configuration.

Reads DATABASE_URL from the environment (with fallback to alembic.ini)
and imports the SQLAlchemy metadata from the ORM models so that
``alembic revision --autogenerate`` works correctly.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import ORM models so their metadata is registered on Base.metadata
from regulatory.db.models import Base  # noqa: F401

# Alembic Config object
config = context.config

# Wire up Python logging from the ini file
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Use DATABASE_URL env var if set, overriding alembic.ini value
db_url = os.environ.get("DATABASE_URL", "")
if db_url:
    # Alembic uses a sync driver; strip asyncpg if someone set the async URL
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    db_url = db_url.replace("postgres://", "postgresql://")
    config.set_main_option("sqlalchemy.url", db_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no DB connection required)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (live DB connection)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
