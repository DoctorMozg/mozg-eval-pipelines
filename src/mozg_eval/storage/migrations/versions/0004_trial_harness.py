"""The harness each trial loaded, by name and content hash."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    with op.batch_alter_table("trials") as trials:
        trials.add_column(sa.Column("harness", sa.String))
        trials.add_column(sa.Column("harness_digest", sa.String))


def downgrade() -> None:
    with op.batch_alter_table("trials") as trials:
        trials.drop_column("harness_digest")
        trials.drop_column("harness")
