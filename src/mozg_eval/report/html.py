"""Static HTML report of one stored run, with the workspace's pass-rate trend across runs."""

import logging
from importlib.resources import files
from typing import Literal

from jinja2 import Environment, PackageLoader
from markupsafe import Markup
from pydantic import JsonValue

from mozg_eval.grading.stats import SuiteStats, wilson
from mozg_eval.storage.base import ResultStore, RunRecord, TrialRecord

log = logging.getLogger(__name__)

_SCRIPTS = ("vega.min.js", "vega-lite.min.js", "vega-embed.min.js")
_OUTCOME_COLORS = {"pass": "#2e7d32", "fail": "#c62828", "error": "#6d4c41"}

type Outcome = Literal["pass", "fail", "error"]
type Spec = dict[str, JsonValue]


def outcome(trial: TrialRecord) -> Outcome:
    if trial.status in ("error", "timeout"):
        return "error"
    return "pass" if trial.passed else "fail"


def heatmap(trials: list[TrialRecord]) -> Spec:
    rows: list[JsonValue] = [
        {"case": t.label, "repeat": t.repeat, "outcome": outcome(t)} for t in trials
    ]
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
        "data": {"values": rows},
        "mark": {"type": "rect", "stroke": "white"},
        "encoding": {
            "x": {"field": "repeat", "type": "ordinal", "title": "repeat"},
            "y": {"field": "case", "type": "nominal", "title": None},
            "color": {
                "field": "outcome",
                "type": "nominal",
                "scale": {
                    "domain": list(_OUTCOME_COLORS),
                    "range": list(_OUTCOME_COLORS.values()),
                },
            },
            "tooltip": [{"field": "case"}, {"field": "repeat"}, {"field": "outcome"}],
        },
    }


def wilson_bars(stats: SuiteStats) -> Spec:
    rows: list[JsonValue] = []
    for case in stats.cases:
        low, high = wilson(case.passes, case.runs)
        rows.append({"case": case.case, "rate": case.pass_at_1, "low": low, "high": high})
    rate_axis: Spec = {
        "field": "rate",
        "type": "quantitative",
        "title": "pass@1",
        "scale": {"domain": [0, 1]},
    }
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
        "data": {"values": rows},
        "encoding": {"y": {"field": "case", "type": "nominal", "title": None}},
        "layer": [
            {"mark": {"type": "bar", "color": "#90a4ae"}, "encoding": {"x": rate_axis}},
            {
                "mark": {"type": "rule", "color": "#263238"},
                "encoding": {
                    "x": {"field": "low", "type": "quantitative"},
                    "x2": {"field": "high"},
                },
            },
        ],
    }


def trend(points: list[dict[str, JsonValue]]) -> Spec:
    x: Spec = {"field": "started", "type": "temporal", "title": "run started"}
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
        "data": {"values": list(points)},
        "encoding": {"x": x},
        "layer": [
            {
                "mark": {"type": "area", "opacity": 0.25, "color": "#1565c0"},
                "encoding": {
                    "y": {"field": "low", "type": "quantitative", "scale": {"domain": [0, 1]}},
                    "y2": {"field": "high"},
                },
            },
            {
                "mark": {"type": "line", "point": True, "color": "#1565c0"},
                "encoding": {
                    "y": {"field": "rate", "type": "quantitative", "title": "cases passing"},
                    "tooltip": [{"field": "run"}, {"field": "rate", "format": ".0%"}],
                },
            },
        ],
    }


class ReportBuilder:
    def __init__(self, results: ResultStore, ws: str, threshold: float) -> None:
        self.results = results
        self.ws = ws
        self.threshold = threshold
        # Always escape: select_autoescape() keys off the extension, and .j2 is not "html".
        self.env = Environment(loader=PackageLoader("mozg_eval.report"), autoescape=True)

    def build(self, run_id: str | None = None) -> str:
        runs = self.results.list_runs(self.ws)
        if not runs:
            raise LookupError(f"no runs stored in workspace {self.ws}")
        run = runs[-1] if run_id is None else self._find(runs, run_id)
        trials = self.results.list_trials(self.ws, run.id)
        stats = SuiteStats.of(trials, self.threshold)
        grades = self.results.list_grades(self.ws, run.id)
        failures = [
            (t, g)
            for t in sorted(trials, key=lambda t: (t.label, t.repeat))
            for g in grades.get(t.id, [])
            if g.verdict == "fail"
        ]
        static = files("mozg_eval.report") / "static"
        scripts = [Markup(_safe_script((static / name).read_text())) for name in _SCRIPTS]
        log.info("report of run %s: %d trials, trend over %d runs", run.id, len(trials), len(runs))
        return self.env.get_template("report.html.j2").render(
            run=run,
            stats=stats,
            rate=stats.pass_rate(),
            failures=failures,
            scripts=scripts,
            charts={
                "heatmap": heatmap(trials),
                "wilson": wilson_bars(stats),
                "trend": trend(self._trend_points(runs)),
            },
        )

    @staticmethod
    def _find(runs: list[RunRecord], run_id: str) -> RunRecord:
        for run in runs:
            if run.id == run_id:
                return run
        raise LookupError(f"no run {run_id}")

    def _trend_points(self, runs: list[RunRecord]) -> list[dict[str, JsonValue]]:
        points: list[dict[str, JsonValue]] = []
        for run in runs:
            trials = self.results.list_trials(self.ws, run.id)
            if not trials:
                continue
            rate, low, high = SuiteStats.of(trials, self.threshold).pass_rate()
            started = run.started_at.isoformat()
            points.append(
                {"run": run.id, "started": started, "rate": rate, "low": low, "high": high}
            )
        return points


def _safe_script(source: str) -> str:
    """Inline scripts end at the first `</script`, so a vendored build must never contain one."""
    return source.replace("</script", "<\\/script")
