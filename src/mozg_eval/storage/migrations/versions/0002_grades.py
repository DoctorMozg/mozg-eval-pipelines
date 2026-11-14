"""Grades per trial, and each trial's overall verdict."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    with op.batch_alter_table("trials") as trials:
        trials.add_column(sa.Column("passed", sa.Boolean))
    op.create_table(
        "grades",
        sa.Column("workspace_id", sa.String, primary_key=True),
        sa.Column("trial_id", sa.String, primary_key=True),
        sa.Column("name", sa.String, primary_key=True),
        sa.Column("verdict", sa.String, nullable=False),
        sa.Column("detail", sa.String, nullable=False),
        sa.Column("weight", sa.Float, nullable=False),
        sa.Column("arm", sa.String, nullable=False),
        sa.ForeignKeyConstraint(["workspace_id", "trial_id"], ["trials.workspace_id", "trials.id"]),
    )


def downgrade() -> None:
    op.drop_table("grades")
    with op.batch_alter_table("trials") as trials:
        trials.drop_column("passed")
