"""Workspaces, runs and trials."""

import sqlalchemy as sa
from alembic import op

from mozg_eval.storage.sql import UtcDateTime

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("created_at", UtcDateTime, nullable=False),
    )
    op.create_table(
        "runs",
        sa.Column("workspace_id", sa.String, sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("started_at", UtcDateTime, nullable=False),
        sa.Column("trigger", sa.String, nullable=False),
        sa.Column("git_sha", sa.String),
    )
    op.create_table(
        "trials",
        sa.Column("workspace_id", sa.String, primary_key=True),
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("run_id", sa.String, nullable=False),
        sa.Column("case_id", sa.String, nullable=False),
        sa.Column("arm", sa.String, nullable=False),
        sa.Column("repeat", sa.Integer, nullable=False),
        sa.Column("driver", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("cost_usd", sa.Float),
        sa.ForeignKeyConstraint(["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"]),
    )
    op.create_index("ix_trials_run", "trials", ["workspace_id", "run_id"])


def downgrade() -> None:
    op.drop_table("trials")
    op.drop_table("runs")
    op.drop_table("workspaces")
