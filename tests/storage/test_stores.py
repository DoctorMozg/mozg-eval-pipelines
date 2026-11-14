import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import IntegrityError

from mozg_eval.events.models import End, Event, Message
from mozg_eval.grading.models import GradeResult
from mozg_eval.storage.base import (
    DEFAULT_WORKSPACE,
    ArtifactNotFoundError,
    ArtifactRef,
    ArtifactStore,
    EventStore,
    ResultStore,
    RunRecord,
    TrialRecord,
)
from mozg_eval.storage.config import StorageConfig, open_stores
from mozg_eval.storage.disk import DiskArtifactStore, DiskEventStore
from mozg_eval.storage.sql import SqlResultStore, metadata

T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def event(seq: int, run: str = "r1", trial: str = "t1", text: str = "hi") -> Event:
    return Event(
        run=run,
        trial=trial,
        seq=seq,
        t=T0 + timedelta(seconds=seq),
        payload=Message(role="assistant", text=text),
    )


def collect[TItem](items: AsyncIterator[TItem]) -> list[TItem]:
    async def drain() -> list[TItem]:
        return [item async for item in items]

    return asyncio.run(drain())


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


# One contract suite per store: each new backend joins the params list and must pass unchanged.
@pytest.fixture(params=["disk"])
def events(tmp_path: Path) -> EventStore:
    return DiskEventStore(tmp_path / "events")


@pytest.fixture(params=["disk"])
def artifacts(tmp_path: Path) -> ArtifactStore:
    return DiskArtifactStore(tmp_path / "artifacts")


@pytest.fixture(params=["sqlite"])
def results(tmp_path: Path) -> Iterator[ResultStore]:
    store = SqlResultStore(f"sqlite:///{tmp_path / 'mzeval.db'}")
    yield store
    store.close()


class TestEventStoreContract:
    def test_reads_back_in_seq_order(self, events: EventStore) -> None:
        stored = [event(1), event(2, text="second"), event(3, text="third")]
        asyncio.run(events.append("local", "r1", "t1", stored))

        assert collect(events.read("local", "r1", "t1")) == stored
        assert collect(events.read("local", "r1", "t1", after_seq=2)) == stored[2:]

    def test_same_trial_and_seq_twice_is_stored_once(self, events: EventStore) -> None:
        asyncio.run(events.append("local", "r1", "t1", [event(1), event(2)]))
        # A resumed push resends the tail it isn't sure arrived.
        asyncio.run(events.append("local", "r1", "t1", [event(2, text="resent"), event(3)]))

        assert [e.seq for e in collect(events.read("local", "r1", "t1"))] == [1, 2, 3]
        assert collect(events.read("local", "r1", "t1"))[1] == event(2)

    def test_dedup_survives_a_new_store_instance(self, tmp_path: Path) -> None:
        asyncio.run(DiskEventStore(tmp_path).append("local", "r1", "t1", [event(1)]))
        reopened = DiskEventStore(tmp_path)
        asyncio.run(reopened.append("local", "r1", "t1", [event(1), event(2)]))

        assert [e.seq for e in collect(reopened.read("local", "r1", "t1"))] == [1, 2]

    def test_rejects_a_gap_in_seq(self, events: EventStore) -> None:
        asyncio.run(events.append("local", "r1", "t1", [event(1)]))

        with pytest.raises(ValueError, match="seq 3 follows 1"):
            asyncio.run(events.append("local", "r1", "t1", [event(3)]))

    def test_rejects_an_event_of_another_trial(self, events: EventStore) -> None:
        with pytest.raises(ValueError, match="r1/t2 appended to r1/t1"):
            asyncio.run(events.append("local", "r1", "t1", [event(1, trial="t2")]))

    def test_rejects_an_id_that_escapes_the_store(self, events: EventStore) -> None:
        with pytest.raises(ValueError, match=r"run id '\.\.'"):
            collect(events.read("local", "..", "t1"))

    def test_workspaces_are_separate(self, events: EventStore) -> None:
        asyncio.run(events.append("a", "r1", "t1", [event(1)]))

        assert collect(events.read("b", "r1", "t1")) == []

    def test_delete_run_removes_its_events_and_allows_a_rerun(self, events: EventStore) -> None:
        asyncio.run(events.append("local", "r1", "t1", [event(1), event(2)]))
        asyncio.run(events.delete_run("local", "r1"))

        assert collect(events.read("local", "r1", "t1")) == []
        asyncio.run(events.append("local", "r1", "t1", [event(1)]))
        assert len(collect(events.read("local", "r1", "t1"))) == 1

    def test_end_payload_round_trips(self, events: EventStore) -> None:
        end = Event(run="r1", trial="t1", seq=1, t=T0, payload=End(status="timeout", reason="x"))
        asyncio.run(events.append("local", "r1", "t1", [end]))

        assert collect(events.read("local", "r1", "t1")) == [end]


class TestArtifactStoreContract:
    def test_put_then_get_returns_the_same_bytes(self, artifacts: ArtifactStore) -> None:
        ref = ArtifactRef(run="r1", trial="t1", name="raw.jsonl")
        asyncio.run(artifacts.put("local", ref, chunks(b"line 1\n", b"line 2\n")))

        assert b"".join(collect(artifacts.get("local", ref))) == b"line 1\nline 2\n"

    def test_put_replaces_an_existing_artifact(self, artifacts: ArtifactStore) -> None:
        ref = ArtifactRef(run="r1", name="report/index.html")
        asyncio.run(artifacts.put("local", ref, chunks(b"old")))
        asyncio.run(artifacts.put("local", ref, chunks(b"new")))

        assert b"".join(collect(artifacts.get("local", ref))) == b"new"

    def test_missing_artifact_and_other_workspace_raise(self, artifacts: ArtifactStore) -> None:
        ref = ArtifactRef(run="r1", trial="t1", name="raw.jsonl")
        asyncio.run(artifacts.put("a", ref, chunks(b"x")))

        with pytest.raises(ArtifactNotFoundError):
            collect(artifacts.get("b", ref))

    @pytest.mark.parametrize("name", ["../escape", "/abs", "a//b", ""])
    def test_ref_rejects_unsafe_names(self, name: str) -> None:
        with pytest.raises(ValueError, match="artifact name segment"):
            ArtifactRef(run="r1", name=name)


def run_record(run_id: str, minutes: int = 0) -> RunRecord:
    return RunRecord(id=run_id, started_at=T0 + timedelta(minutes=minutes), trigger="local")


def trial_record(trial_id: str, run: str = "r1") -> TrialRecord:
    return TrialRecord(
        id=trial_id, run=run, case="c1", arm="with", repeat=1, driver="claude", status="ok"
    )


class TestResultStoreContract:
    def test_default_workspace_exists_on_first_start(self, results: ResultStore) -> None:
        results.add_run(DEFAULT_WORKSPACE, run_record("r1"))

        assert results.get_run(DEFAULT_WORKSPACE, "r1") == run_record("r1")

    def test_runs_list_oldest_first(self, results: ResultStore) -> None:
        results.add_run("local", run_record("r2", minutes=5))
        results.add_run("local", run_record("r1"))

        assert [r.id for r in results.list_runs("local")] == ["r1", "r2"]

    def test_trials_belong_to_their_run(self, results: ResultStore) -> None:
        results.add_run("local", run_record("r1"))
        results.add_run("local", run_record("r2"))
        results.add_trial("local", trial_record("t1"))
        results.add_trial("local", trial_record("t2", run="r2"))

        assert results.list_trials("local", "r1") == [trial_record("t1")]

    def test_trial_of_unknown_run_is_rejected(self, results: ResultStore) -> None:
        with pytest.raises(IntegrityError):
            results.add_trial("local", trial_record("t1", run="nope"))

    def test_workspace_b_cannot_read_workspace_a(self, results: ResultStore) -> None:
        results.ensure_workspace("a")
        results.ensure_workspace("b")
        results.add_run("a", run_record("r1"))
        results.add_trial("a", trial_record("t1"))

        assert results.get_run("b", "r1") is None
        assert results.list_runs("b") == []
        assert results.list_trials("b", "r1") == []
        # Same ids in another workspace don't collide.
        results.add_run("b", run_record("r1"))
        assert results.list_trials("b", "r1") == []


class TestGradeStorage:
    def test_grades_round_trip_per_trial_and_stay_in_their_workspace(
        self, results: ResultStore
    ) -> None:
        grades = [
            GradeResult(name="expect", verdict="pass", detail="fired: x", arm="with"),
            GradeResult(name="tests", verdict="fail", detail="exit 1", weight=2.0),
        ]
        results.add_run("local", run_record("r1"))
        results.add_trial("local", trial_record("t1").model_copy(update={"passed": False}))
        results.add_grades("local", "t1", grades)

        assert results.list_grades("local", "r1") == {"t1": grades}
        assert results.list_trials("local", "r1")[0].passed is False
        results.ensure_workspace("b")
        assert results.list_grades("b", "r1") == {}


class TestSqlSchema:
    def test_migrations_match_the_tables_the_store_queries(self, tmp_path: Path) -> None:
        store = SqlResultStore(f"sqlite:///{tmp_path / 'mzeval.db'}")
        with store.engine.connect() as connection:
            diff = compare_metadata(MigrationContext.configure(connection), metadata)
        store.close()

        assert diff == []

    def test_reopening_keeps_data_and_wal_mode(self, tmp_path: Path) -> None:
        url = f"sqlite:///{tmp_path / 'db' / 'mzeval.db'}"
        SqlResultStore(url).add_run("local", run_record("r1"))
        store = SqlResultStore(url)
        with store.engine.connect() as connection:
            mode = connection.execute(sa.text("PRAGMA journal_mode")).scalar()

        assert store.get_run("local", "r1") == run_record("r1")
        assert mode == "wal"
        store.close()


class TestStorageConfig:
    def test_defaults_live_under_dot_mzeval(self, tmp_path: Path) -> None:
        config = StorageConfig.load(tmp_path, env={})

        assert config.results == f"sqlite:///{tmp_path / '.mzeval' / 'mzeval.db'}"
        assert config.events == (tmp_path / ".mzeval" / "events").as_uri()

    def test_env_overrides_toml(self, tmp_path: Path) -> None:
        (tmp_path / "mzeval.toml").write_text(
            '[storage]\nevents = "file:///toml/events"\nartifacts = "file:///toml/art"\n'
        )
        config = StorageConfig.load(tmp_path, env={"MZEVAL_EVENTS": "file:///env/events"})

        assert config.events == "file:///env/events"
        assert config.artifacts == "file:///toml/art"

    def test_unknown_key_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "mzeval.toml").write_text('[storage]\nevent = "file:///typo"\n')

        with pytest.raises(ValueError, match="bad storage config"):
            StorageConfig.load(tmp_path, env={})

    def test_opened_stores_write_where_the_urls_point(self, tmp_path: Path) -> None:
        stores = open_stores(StorageConfig.load(tmp_path, env={}))
        asyncio.run(stores.events.append("local", "r1", "t1", [event(1)]))
        stores.results.close()

        assert (tmp_path / ".mzeval" / "events" / "local" / "r1" / "t1" / "events.jsonl").exists()
        assert (tmp_path / ".mzeval" / "mzeval.db").exists()

    @pytest.mark.parametrize(
        ("key", "url"), [("results", "postgresql://u:secret@h/db"), ("events", "s3://b/e")]
    )
    def test_unsupported_backend_is_named(self, tmp_path: Path, key: str, url: str) -> None:
        config = StorageConfig.load(tmp_path, env={f"MZEVAL_{key.upper()}": url})

        with pytest.raises(ValueError, match="supported so far") as error:
            open_stores(config)
        assert "secret" not in str(error.value)
