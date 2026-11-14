import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import IngestStub
from typer.testing import CliRunner

from mozg_eval.cli import app
from mozg_eval.events.models import End, Event, Message
from mozg_eval.grading.models import GradeResult, JudgeSpend
from mozg_eval.storage.base import ArtifactRef, RunRecord, TrialRecord
from mozg_eval.storage.config import StorageConfig, open_stores

T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


async def chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


@pytest.fixture
def stored_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """One trial with a grade, a judge call, two events and a raw stream; no stderr.log."""
    monkeypatch.chdir(tmp_path)
    stores = open_stores(StorageConfig.load(tmp_path, env={}))
    results = stores.results
    results.add_run("local", RunRecord(id="r1", started_at=T0, trigger="local"))
    results.add_trial(
        "local",
        TrialRecord(
            id="t1", run="r1", case="c", arm="with", repeat=1, driver="claude", status="ok",
            passed=True,
        ),
    )  # fmt: skip
    results.add_grades("local", "t1", [GradeResult(name="g", verdict="pass")])
    results.add_spend(
        "local", "t1", [JudgeSpend(model="m", input_tokens=3, output_tokens=1, cost_usd=0.001)]
    )
    events = [
        Event(run="r1", trial="t1", seq=1, t=T0, payload=Message(role="assistant", text="hi")),
        Event(run="r1", trial="t1", seq=2, t=T0, payload=End(status="ok")),
    ]

    async def store() -> None:
        await stores.events.append("local", "r1", "t1", events)
        ref = ArtifactRef(run="r1", trial="t1", name="raw.jsonl")
        await stores.artifacts.put("local", ref, chunks(b'{"raw": 1}\n'))

    asyncio.run(store())
    results.close()
    return "r1"


class TestPush:
    def test_sends_run_trial_events_and_artifacts_with_the_token(
        self, stored_run: str, ingest_server: IngestStub
    ) -> None:
        result = CliRunner().invoke(app, ["push", stored_run])

        assert result.exit_code == 0, result.output
        assert f"pushed run r1: 1 trials to {ingest_server.url}" in result.output
        sent = ingest_server.requests
        assert [(r.method, r.path) for r in sent] == [
            ("PUT", "/v1/runs/r1"),
            ("PUT", "/v1/runs/r1/trials/t1"),
            ("POST", "/v1/runs/r1/trials/t1/events"),
            ("PUT", "/v1/runs/r1/trials/t1/artifacts/raw.jsonl"),
        ]
        assert {r.auth for r in sent} == {"Bearer secret-token"}
        trial = json.loads(sent[1].body)
        assert trial["grades"][0]["verdict"] == "pass"
        assert trial["judge_spend"][0]["cost_usd"] == 0.001
        assert [e["seq"] for e in json.loads(sent[2].body)] == [1, 2]
        assert sent[3].body == b'{"raw": 1}\n'

    def test_server_error_exits_2_without_leaking_the_token(
        self, stored_run: str, ingest_server: IngestStub
    ) -> None:
        ingest_server.fail = "/v1/runs/r1/trials"

        result = CliRunner().invoke(app, ["push", stored_run])

        assert result.exit_code == 2
        assert "push failed: PUT /v1/runs/r1/trials/t1: HTTP 500" in result.output
        assert "secret-token" not in result.output

    def test_missing_server_settings_and_unknown_run_exit_2(
        self, stored_run: str, ingest_server: IngestStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unknown = CliRunner().invoke(app, ["push", "nope"])
        monkeypatch.delenv("MZEVAL_TOKEN")
        unset = CliRunner().invoke(app, ["push", stored_run])

        assert (unknown.exit_code, unset.exit_code) == (2, 2)
        assert "no stored run 'nope'" in unknown.output
        assert "set MZEVAL_TOKEN to push results to a server" in unset.output
