from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from mozg_eval.importers.claude_plugin_eval import (
    CaseImportError,
    ClaudePluginEvalImporter,
    Conversion,
)
from mozg_eval.spec.loader import CASE_FILENAME, CaseLoader

type Convert = Callable[..., Conversion]

SKILL = '"skill"\\s*:\\s*"(?:[\\w-]+:)?{name}"'


def routing(name: str, skill: str, **limits: int | str) -> dict[str, Any]:
    return {
        "name": name,
        "type": "tool_used",
        "tool": "Skill",
        "input_match": SKILL.format(name=skill),
        **limits,
    }


def case_11(graders: list[dict[str, Any]], **extra: object) -> dict[str, Any]:
    return {
        "schema_version": "1.1",
        "name": "sample",
        "tags": ["routing"],
        "plugins": ["../../plugins/p"],
        "runs": 3,
        "execution": {
            "prompt": "Do the thing.",
            "max_turns": 8,
            "timeout_seconds": 240,
            "allowed_tools": ["Skill", "Bash", "AskUserQuestion"],
        },
        "graders": graders,
        **extra,
    }


@pytest.fixture
def importer() -> ClaudePluginEvalImporter:
    return ClaudePluginEvalImporter()


@pytest.fixture
def convert(importer: ClaudePluginEvalImporter, tmp_path: Path) -> Convert:
    def run(data: dict[str, Any], case_dir: str = "evals/p/sample") -> Conversion:
        directory = tmp_path / case_dir
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "case.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        return importer.convert(directory / "case.yaml")

    return run


class TestRouting:
    def test_fire_case_becomes_an_expected_skill(self, convert: Convert) -> None:
        result = convert(case_11([routing("fires", "render")], tags=["fire"]))

        assert result.case["kind"] == "fire"
        assert result.case["expect"] == {"skill": "render"}
        assert "graders" not in result.case

    def test_near_miss_keeps_the_owner_it_asserted(self, convert: Convert) -> None:
        graders = [
            routing("silent", "render", min=0, max=0, arm="both"),
            routing("owner", "verify", arm="both"),
        ]

        result = convert(case_11(graders, tags=["near-miss"]))

        assert result.case["expect"] == {
            "skill": {"not": "render", "owner": "verify"},
            "arm": "both",
        }
        assert "graders" not in result.case

    def test_owner_scored_in_another_arm_stays_a_grader(self, convert: Convert) -> None:
        graders = [
            routing("silent", "render", min=0, max=0, arm="both"),
            routing("owner", "verify"),
        ]

        result = convert(case_11(graders, tags=["negative"]))

        assert result.case["expect"] == {"skill": {"not": "render"}, "arm": "both"}
        assert result.case["graders"] == [{"name": "owner", "type": "skill", "skill": "verify"}]

    def test_count_limit_survives_next_to_the_label(self, convert: Convert) -> None:
        result = convert(case_11([routing("fires", "render", min=1, max=2)], tags=["fire"]))

        assert result.case["expect"] == {"skill": "render"}
        assert result.case["graders"] == [
            {"name": "fires", "type": "skill", "skill": "render", "min": 1, "max": 2}
        ]

    def test_fire_tag_without_a_routing_grader_is_flagged(self, convert: Convert) -> None:
        grader = {"name": "says-ok", "type": "regex", "pattern": "ok"}

        result = convert(case_11([grader], tags=["fire"]))

        assert result.case["kind"] == "behaviour"
        assert [flag.grader for flag in result.flags] == ["<case>"]


class TestGraders:
    @pytest.mark.parametrize(
        ("match", "limits"),
        [
            ("contains", {}),
            ("not_contains", {"min": 0, "max": 0}),
            ("count:2", {"min": 2, "max": 2}),
        ],
    )
    def test_regex_match_modes_become_counts(
        self, convert: Convert, match: str, limits: dict[str, int]
    ) -> None:
        grader = {"name": "g", "type": "regex", "pattern": "x", "match": match, "flags": "i"}

        [converted] = convert(case_11([grader], tags=["gate"])).case["graders"]

        assert converted == {
            "name": "g",
            "type": "regex",
            "pattern": "x",
            "target": "final",
            "flags": "i",
            **limits,
        }

    def test_file_target_and_created_files(self, convert: Convert) -> None:
        graders: list[dict[str, Any]] = [
            {
                "name": "a",
                "type": "regex",
                "pattern": "x",
                "target": {"source": "file", "path": "out.md"},
            },
            {"name": "b", "type": "regex", "pattern": "y", "target": "files"},
        ]

        converted = convert(case_11(graders, tags=["gate"])).case["graders"]

        assert [g["target"] for g in converted] == [{"file": "out.md"}, "created_files"]

    def test_trace_regex_is_kept_for_claude_and_flagged(self, convert: Convert) -> None:
        grader = {"name": "hook", "type": "regex", "pattern": "hook_response", "target": "trace"}

        result = convert(case_11([grader], tags=["hooks"]))

        assert result.case["graders"] == [
            {"name": "hook", "type": "raw_regex", "driver": "claude", "pattern": "hook_response"}
        ]
        assert len(result.flags) == 1

    def test_file_path_input_match_becomes_a_path_regex(self, convert: Convert) -> None:
        grader = {
            "name": "no-write",
            "type": "tool_used",
            "tool": "Write",
            "input_match": '"file_path"\\s*:\\s*"[^"]*/\\.mz/state\\.md"',
            "min": 0,
            "max": 0,
        }

        result = convert(case_11([grader], tags=["gate"]))

        assert result.case["graders"] == [
            {
                "name": "no-write",
                "type": "tool_called",
                "kind": "write",
                "path_match": '^[^"]*/\\.mz/state\\.md$',
                "min": 0,
                "max": 0,
            }
        ]
        assert result.flags == []

    def test_llm_criteria_become_one_flagged_judge_item(self, convert: Convert) -> None:
        grader = {"name": "j", "type": "llm", "criteria": "PASS if polite.\n", "weight": 2}

        result = convert(case_11([grader], tags=["trap"]))

        assert result.case["graders"] == [
            {"name": "j", "type": "judge", "checklist": ["PASS if polite."], "weight": 2}
        ]
        assert len(result.flags) == 1

    def test_unknown_tool_is_an_error(self, convert: Convert) -> None:
        data = case_11([routing("fires", "render")], tags=["fire"])
        data["execution"]["allowed_tools"] = ["WebFetch"]

        with pytest.raises(CaseImportError, match="WebFetch"):
            convert(data)


class TestCaseFields:
    def test_execution_and_tools_are_renamed(self, convert: Convert) -> None:
        result = convert(case_11([routing("fires", "render")], tags=["fire"]))

        assert result.case["execution"] == {
            "prompt": "Do the thing.",
            "tools": ["skill", "shell", "ask_user"],
            "max_turns": 8,
            "timeout_s": 240,
            "runs": 3,
        }

    def test_missing_plugins_resolve_to_the_enclosing_plugin(
        self, convert: Convert, tmp_path: Path
    ) -> None:
        (tmp_path / "plugins/p/.claude-plugin").mkdir(parents=True)
        (tmp_path / "plugins/p/.claude-plugin/plugin.json").write_text("{}")
        data = case_11([routing("fires", "render")], tags=["fire"])
        del data["plugins"]

        result = convert(data, case_dir="plugins/p/evals/sample")

        assert result.case["bundle"] == ["../.."]


class TestImportTree:
    def test_writes_loadable_cases_next_to_the_originals(
        self, importer: ClaudePluginEvalImporter, convert: Convert, tmp_path: Path
    ) -> None:
        convert(case_11([routing("fires", "render")], tags=["fire"]))

        summary = importer.import_tree([tmp_path / "evals"])

        [written] = summary.written
        assert written == tmp_path / "evals/p/sample" / CASE_FILENAME
        assert CaseLoader().load(written).expect.skill == "render"

    def test_existing_v2_case_is_skipped_unless_forced(
        self, importer: ClaudePluginEvalImporter, convert: Convert, tmp_path: Path
    ) -> None:
        convert(case_11([routing("fires", "render")], tags=["fire"]))
        target = tmp_path / "evals/p/sample" / CASE_FILENAME
        target.write_text("hand-edited")

        skipped = importer.import_tree([tmp_path / "evals"])
        forced = importer.import_tree([tmp_path / "evals"], force=True)

        assert (skipped.existing, forced.written) == ([target], [target])
        assert target.read_text() != "hand-edited"

    def test_out_dir_gets_absolute_paths(
        self, importer: ClaudePluginEvalImporter, convert: Convert, tmp_path: Path
    ) -> None:
        convert(case_11([routing("fires", "render")], tags=["fire"]))

        [written] = importer.import_tree([tmp_path / "evals"], tmp_path / "out").written

        assert written == tmp_path / "out/evals/p/sample" / CASE_FILENAME
        assert CaseLoader().load(written).bundle == [(tmp_path / "evals/plugins/p").as_posix()]
