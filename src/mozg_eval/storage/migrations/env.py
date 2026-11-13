"""Alembic runs migrations on the connection `SqlResultStore` hands it; there is no alembic.ini."""

from alembic import context

from mozg_eval.storage.sql import metadata

context.configure(
    connection=context.config.attributes["connection"],
    target_metadata=metadata,
    render_as_batch=True,  # SQLite can only alter tables by copying them
)
with context.begin_transaction():
    context.run_migrations()
