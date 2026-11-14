import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mozg_eval.cli import app
from mozg_eval.grading.models import GradeResult, Verdict
from mozg_eval.report.html import ReportBuilder
from mozg_eval.storage.base import RunRecord, TrialRecord
from mozg_eval.storage.sql import SqlResultStore

T0 = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


def add_run(store: SqlResultStore, run_id: str, day: int, outcomes: dict[str, list[bool]]) -> None:
    store.add_run("local", RunRecord(id=run_id, started_at=T0 + timedelta(days=day), trigger="ci"))
    for case, passes in outcomes.items():
        for repeat, passed in enumerate(passes, start=1):
            trial_id = f"{run_id}-{case}-{repeat}"
            store.add_trial(
                "local",
                TrialRecord(
                    id=trial_id,
                    run=run_id,
                    case=case,
                    arm="with",
                    repeat=repeat,
                    driver="claude",
                    status="ok",
                    passed=passed,
                ),
            )
            verdict: Verdict = "pass" if passed else "fail"
            detail = "fired: <script>alert(1)</script>"
            store.add_grades(
                "local", trial_id, [GradeResult(name="expect", verdict=verdict, detail=detail)]
            )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqlResultStore]:
    """Two runs a day apart: the flaky case recovers in the second."""
    store = SqlResultStore(f"sqlite:///{tmp_path / '.mzeval' / 'mzeval.db'}")
    add_run(store, "r1", 0, {"steady": [True, True, True], "flaky": [True, False, True]})
    add_run(store, "r2", 1, {"steady": [True, True, True], "flaky": [True, True, True]})
    yield store
    store.close()


def charts_of(html: str) -> dict[str, dict[str, object]]:
    found = re.search(r"const charts = (.*);\n", html)
    assert found is not None
    charts: dict[str, dict[str, object]] = json.loads(found.group(1))
    return charts


def values(chart: dict[str, object]) -> list[dict[str, object]]:
    data = chart["data"]
    assert isinstance(data, dict)
    rows: list[dict[str, object]] = data["values"]
    return rows


class TestReport:
    def test_latest_run_has_heatmap_wilson_bars_and_a_two_point_trend(
        self, store: SqlResultStore
    ) -> None:
        charts = charts_of(ReportBuilder(store, "local", threshold=1.0).build())

        assert len(values(charts["heatmap"])) == 6
        wilson = {row["case"]: row for row in values(charts["wilson"])}
        assert wilson["flaky"]["rate"] == 1.0
        assert wilson["flaky"]["low"] == pytest.approx(0.4385, abs=1e-4)
        trend = values(charts["trend"])
        assert [(p["run"], p["rate"]) for p in trend] == [("r1", 0.5), ("r2", 1.0)]

    def test_older_run_lists_its_failures_escaped(self, store: SqlResultStore) -> None:
        html = ReportBuilder(store, "local", threshold=1.0).build("r1")

        assert "Flaky: flaky." in html
        assert "fired: &lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "<script>alert(1)" not in html

    def test_unknown_run_is_a_lookup_error(self, store: SqlResultStore) -> None:
        with pytest.raises(LookupError, match="no run nope"):
            ReportBuilder(store, "local", threshold=1.0).build("nope")


class TestReportCommand:
    def test_writes_the_report(
        self, store: SqlResultStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(app, ["report", "--out", "out.html"])

        assert result.exit_code == 0, result.output
        assert 'id="trend"' in (tmp_path / "out.html").read_text()

    def test_empty_store_exits_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(app, ["report"])

        assert result.exit_code == 2
        assert "no runs stored" in result.output
