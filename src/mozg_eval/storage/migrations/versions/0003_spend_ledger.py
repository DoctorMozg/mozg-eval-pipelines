"""Spend ledger: one row per paid judge call, kept across rejudges."""

import sqlalchemy as sa
from alembic import op

from mozg_eval.storage.sql import UtcDateTime

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    op.create_table(
        "spend_ledger",
        sa.Column("workspace_id", sa.String, primary_key=True),
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("trial_id", sa.String, nullable=False),
        sa.Column("recorded_at", UtcDateTime, nullable=False),
        sa.Column("model", sa.String, nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False),
        sa.Column("output_tokens", sa.Integer, nullable=False),
        sa.Column("cost_usd", sa.Float),
        sa.ForeignKeyConstraint(["workspace_id", "trial_id"], ["trials.workspace_id", "trials.id"]),
        sa.Index("ix_spend_ledger_trial", "workspace_id", "trial_id"),
    )


def downgrade() -> None:
    op.drop_table("spend_ledger")
