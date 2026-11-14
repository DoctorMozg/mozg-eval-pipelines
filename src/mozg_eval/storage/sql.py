"""`ResultStore` on SQLAlchemy Core: SQLite now, Postgres later by URL. Alembic owns the schema."""

import logging
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection

from mozg_eval.grading.models import GradeResult, JudgeSpend
from mozg_eval.storage.base import DEFAULT_WORKSPACE, RunRecord, TrialRecord, check_id, new_id

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
    sa.Column("passed", sa.Boolean),
    sa.ForeignKeyConstraint(["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"]),
    sa.Index("ix_trials_run", "workspace_id", "run_id"),
)

grades = sa.Table(
    "grades",
    metadata,
    sa.Column("workspace_id", sa.String, primary_key=True),
    sa.Column("trial_id", sa.String, primary_key=True),
    sa.Column("name", sa.String, primary_key=True),
    sa.Column("verdict", sa.String, nullable=False),
    sa.Column("detail", sa.String, nullable=False),
    sa.Column("weight", sa.Float, nullable=False),
    sa.Column("arm", sa.String, nullable=False),
    sa.ForeignKeyConstraint(["workspace_id", "trial_id"], ["trials.workspace_id", "trials.id"]),
)


spend_ledger = sa.Table(
    "spend_ledger",
    metadata,
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
                    passed=trial.passed,
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
                trials.c.passed,
            )
            .where(trials.c.workspace_id == ws, trials.c.run_id == run)
            .order_by(trials.c.id)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(query).all()
        return [TrialRecord.model_validate(row._asdict()) for row in rows]

    def add_grades(self, ws: str, trial: str, results: Sequence[GradeResult]) -> None:
        if not results:
            return
        rows = [
            {"workspace_id": ws, "trial_id": trial, **result.model_dump()} for result in results
        ]
        with self.engine.begin() as connection:
            connection.execute(grades.insert(), rows)

    def regrade(self, ws: str, trial: str, results: Sequence[GradeResult], passed: bool) -> None:
        names = [result.name for result in results]
        mine = (grades.c.workspace_id == ws) & (grades.c.trial_id == trial)
        with self.engine.begin() as connection:
            updated = connection.execute(
                trials.update()
                .where(trials.c.workspace_id == ws, trials.c.id == trial)
                .values(passed=passed)
            )
            if updated.rowcount != 1:
                raise LookupError(f"no trial {trial!r} in workspace {ws!r}")
            connection.execute(grades.delete().where(mine & grades.c.name.in_(names)))
            if results:
                rows = [{"workspace_id": ws, "trial_id": trial, **r.model_dump()} for r in results]
                connection.execute(grades.insert(), rows)

    def list_grades(self, ws: str, run: str) -> dict[str, list[GradeResult]]:
        """Grades of every trial in the run, keyed by trial id."""
        query = (
            sa.select(grades)
            .join(
                trials,
                (trials.c.workspace_id == grades.c.workspace_id)
                & (trials.c.id == grades.c.trial_id),
            )
            .where(grades.c.workspace_id == ws, trials.c.run_id == run)
            .order_by(grades.c.trial_id, grades.c.name)
        )
        found: dict[str, list[GradeResult]] = {}
        with self.engine.connect() as connection:
            for row in connection.execute(query).mappings():
                result = GradeResult.model_validate({k: row[k] for k in GradeResult.model_fields})
                found.setdefault(row["trial_id"], []).append(result)
        return found

    def add_spend(self, ws: str, trial: str, spend: Sequence[JudgeSpend]) -> None:
        if not spend:
            return
        now = datetime.now(UTC)
        rows = [
            {"workspace_id": ws, "id": new_id(), "trial_id": trial, "recorded_at": now}
            | item.model_dump()
            for item in spend
        ]
        with self.engine.begin() as connection:
            connection.execute(spend_ledger.insert(), rows)

    def list_spend(self, ws: str, run: str) -> dict[str, list[JudgeSpend]]:
        """Judge spend of every trial in the run, keyed by trial id, oldest first."""
        query = (
            sa.select(spend_ledger)
            .join(
                trials,
                (trials.c.workspace_id == spend_ledger.c.workspace_id)
                & (trials.c.id == spend_ledger.c.trial_id),
            )
            .where(spend_ledger.c.workspace_id == ws, trials.c.run_id == run)
            .order_by(spend_ledger.c.id)
        )
        found: dict[str, list[JudgeSpend]] = {}
        with self.engine.connect() as connection:
            for row in connection.execute(query).mappings():
                item = JudgeSpend.model_validate({k: row[k] for k in JudgeSpend.model_fields})
                found.setdefault(row["trial_id"], []).append(item)
        return found
