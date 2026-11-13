"""`ResultStore` on SQLAlchemy Core: SQLite now, Postgres later by URL. Alembic owns the schema."""

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection

from mozg_eval.storage.base import DEFAULT_WORKSPACE, RunRecord, TrialRecord, check_id

log = logging.getLogger(__name__)

MIGRATIONS = Path(__file__).with_name("migrations")


class UtcDateTime(sa.TypeDecorator[datetime]):
    """SQLite drops the timezone, so store UTC and hand back aware datetimes."""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: sa.Dialect) -> datetime | None:
        return None if value is None else value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: sa.Dialect) -> datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=UTC)


# Every key and index starts with workspace_id, so the schema is tenant-ready from day one.
metadata = sa.MetaData()

workspaces = sa.Table(
    "workspaces",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("created_at", UtcDateTime, nullable=False),
)

runs = sa.Table(
    "runs",
    metadata,
    sa.Column("workspace_id", sa.String, sa.ForeignKey("workspaces.id"), primary_key=True),
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("started_at", UtcDateTime, nullable=False),
    sa.Column("trigger", sa.String, nullable=False),
    sa.Column("git_sha", sa.String),
)

trials = sa.Table(
    "trials",
    metadata,
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
    sa.Index("ix_trials_run", "workspace_id", "run_id"),
)


def _sqlite_pragmas(dbapi_connection: sqlite3.Connection, _record: object) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def migrate(connection: Connection) -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


class SqlResultStore:
    def __init__(self, url: str) -> None:
        parsed = sa.make_url(url)
        if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:"):
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        self.engine = sa.create_engine(parsed)
        if parsed.get_backend_name() == "sqlite":
            sa.event.listen(self.engine, "connect", _sqlite_pragmas)
        with self.engine.begin() as connection:
            migrate(connection)
        self.ensure_workspace(DEFAULT_WORKSPACE)

    def close(self) -> None:
        self.engine.dispose()

    def ensure_workspace(self, ws: str) -> None:
        with self.engine.begin() as connection:
            found = connection.execute(
                sa.select(workspaces.c.id).where(workspaces.c.id == check_id(ws, "workspace"))
            ).first()
            if found is None:
                connection.execute(workspaces.insert().values(id=ws, created_at=datetime.now(UTC)))
                log.info("created workspace %s", ws)

    def add_run(self, ws: str, run: RunRecord) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                runs.insert().values(
                    workspace_id=ws,
                    id=check_id(run.id, "run"),
                    started_at=run.started_at,
                    trigger=run.trigger,
                    git_sha=run.git_sha,
                )
            )

    def _runs(self, ws: str) -> sa.Select[tuple[str, datetime, str, str | None]]:
        return sa.select(runs.c.id, runs.c.started_at, runs.c.trigger, runs.c.git_sha).where(
            runs.c.workspace_id == ws
        )

    def get_run(self, ws: str, run: str) -> RunRecord | None:
        with self.engine.connect() as connection:
            row = connection.execute(self._runs(ws).where(runs.c.id == run)).first()
        return None if row is None else RunRecord.model_validate(row._asdict())

    def list_runs(self, ws: str) -> list[RunRecord]:
        with self.engine.connect() as connection:
            rows = connection.execute(self._runs(ws).order_by(runs.c.started_at)).all()
        return [RunRecord.model_validate(row._asdict()) for row in rows]

    def add_trial(self, ws: str, trial: TrialRecord) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                trials.insert().values(
                    workspace_id=ws,
                    id=check_id(trial.id, "trial"),
                    run_id=trial.run,
                    case_id=trial.case,
                    arm=trial.arm,
                    repeat=trial.repeat,
                    driver=trial.driver,
                    status=trial.status,
                    cost_usd=trial.cost_usd,
                )
            )

    def list_trials(self, ws: str, run: str) -> list[TrialRecord]:
        query = (
            sa.select(
                trials.c.id,
                trials.c.run_id.label("run"),
                trials.c.case_id.label("case"),
                trials.c.arm,
                trials.c.repeat,
                trials.c.driver,
                trials.c.status,
                trials.c.cost_usd,
            )
            .where(trials.c.workspace_id == ws, trials.c.run_id == run)
            .order_by(trials.c.id)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(query).all()
        return [TrialRecord.model_validate(row._asdict()) for row in rows]
