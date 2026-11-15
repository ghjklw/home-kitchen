"""Migrations use the same explicitly selected PostgreSQL URL as the application."""

import os

from alembic import context
from sqlalchemy import create_engine, pool

from home_kitchen.store import metadata

config = context.config
database_url = config.attributes.get("database_url") or os.environ["KITCHEN_DATABASE_URL"]

if context.is_offline_mode():
    context.configure(url=database_url, target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(database_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
