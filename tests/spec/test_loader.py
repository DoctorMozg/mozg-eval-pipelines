from collections.abc import Callable
from pathlib import Path

import pytest

from mozg_eval.spec.loader import CASE_FILENAME, CaseLoader, CaseLoadError
from mozg_eval.spec.models import Case, SkillNotExpected

EXAMPLES = Path(__file__).parents[2] / "examples" / "cases"

type WriteCase = Callable[[str], Path]

VALID = """\
schema: mzeval/2
name: render-fire
kind: fire
expect:
  skill: resolve-render
execution:
  prompt: Render the timeline.
"""


@pytest.fixture
def loader() -> CaseLoader:
    return CaseLoader()


@pytest.fixture
def write_case(tmp_path: Path) -> WriteCase:
    def write(text: str) -> Path:
        path = tmp_path / CASE_FILENAME
        path.write_text(text, encoding="utf-8")
        return path

    return write


def issues_of(loader: CaseLoader, path: Path) -> list[str]:
    with pytest.raises(CaseLoadError) as caught:
        loader.load(path)
    return [str(issue) for issue in caught.value.issues]


class TestExamples:
    @pytest.mark.parametrize(
        "path", sorted(EXAMPLES.rglob(CASE_FILENAME)), ids=lambda p: p.parent.name
    )
    def test_every_example_loads(self, loader: CaseLoader, path: Path) -> None:
        case = loader.load(path)

        assert case.name == path.parent.name


class TestDefaults:
    def test_valid_case_gets_1_1_defaults(self, loader: CaseLoader, write_case: WriteCase) -> None:
        case = loader.load(write_case(VALID))

        assert (case.execution.runs, case.execution.max_turns, case.execution.timeout_s) == (
            3,
            10,
            300,
        )

    def test_near_miss_reads_the_not_key(self, loader: CaseLoader, write_case: WriteCase) -> None:
        text = VALID.replace("kind: fire", "kind: near-miss").replace(
            "skill: resolve-render", "skill: { not: resolve-render, owner: resolve-verify }"
        )

        case = loader.load(write_case(text))

        skill = case.expect.skill
        assert isinstance(skill, SkillNotExpected)
        assert (skill.not_, skill.owner) == ("resolve-render", "resolve-verify")

    def test_skill_graders_are_left_out_of_ablation_scores(self) -> None:
        case = Case.model_validate(
            {
                "schema": "mzeval/2",
                "name": "x",
                "kind": "behaviour",
                "execution": {"prompt": "p"},
                "graders": [
                    {"name": "fires", "type": "skill", "skill": "debug"},
                    {"name": "delegates", "type": "subagent", "subagent": "planner"},
                    {"name": "answer", "type": "regex", "pattern": "ok"},
                ],
            }
        )

        assert [grader.arm for grader in case.graders] == ["with", "both", "both"]


class TestErrors:
    def test_unknown_key_is_reported_with_its_line(
        self, loader: CaseLoader, write_case: WriteCase
    ) -> None:
        path = write_case(VALID.replace("  prompt:", "  max_turn: 5\n  prompt:"))

        assert issues_of(loader, path) == [f"{path}:7: unknown key 'max_turn' in execution"]

    def test_unknown_grader_key_points_at_the_grader_line(
        self, loader: CaseLoader, write_case: WriteCase
    ) -> None:
        text = VALID + "graders:\n  - name: answer\n    type: regex\n    patern: ok\n"

        issues = issues_of(loader, write_case(text))

        assert any(":11: unknown key 'patern'" in issue for issue in issues)

    def test_bad_regex_is_rejected(self, loader: CaseLoader, write_case: WriteCase) -> None:
        text = VALID + "graders:\n  - name: answer\n    type: regex\n    pattern: '(unclosed'\n"

        issues = issues_of(loader, write_case(text))

        assert len(issues) == 1
        assert ":9: " in issues[0]
        assert "invalid regex '(unclosed'" in issues[0]

    def test_min_above_max_is_rejected(self, loader: CaseLoader, write_case: WriteCase) -> None:
        text = (
            VALID
            + "graders:\n  - name: two\n    type: skill\n    skill: a\n    min: 3\n    max: 1\n"
        )

        assert "max (1) is below min (3)" in issues_of(loader, write_case(text))[0]

    def test_duplicate_grader_names_are_rejected(
        self, loader: CaseLoader, write_case: WriteCase
    ) -> None:
        grader = "  - name: same\n    type: skill\n    skill: a\n"
        text = VALID + "graders:\n" + grader + grader

        assert "grader names must be unique: same" in issues_of(loader, write_case(text))[0]

    def test_fire_case_without_expected_skill_is_rejected(
        self, loader: CaseLoader, write_case: WriteCase
    ) -> None:
        text = VALID.replace("expect:\n  skill: resolve-render\n", "")
        text += "graders:\n  - name: answer\n    type: regex\n    pattern: ok\n"

        assert "a fire case needs expect.skill" in issues_of(loader, write_case(text))[0]

    def test_invalid_yaml_reports_its_line(self, loader: CaseLoader, write_case: WriteCase) -> None:
        path = write_case(VALID + "graders: [\n")

        [issue] = issues_of(loader, path)

        assert issue.startswith(f"{path}:")
        assert "invalid YAML" in issue

    def test_1_1_case_points_to_the_importer(
        self, loader: CaseLoader, write_case: WriteCase
    ) -> None:
        path = write_case("schema_version: '1.1'\nname: old\n")

        assert issues_of(loader, path) == [
            f"{path}:1: this is a claude plugin eval 1.1 case; convert it with `mzeval import`"
        ]


class TestReferencedPaths:
    def test_missing_bundle_and_fixture_are_reported_with_their_lines(
        self, loader: CaseLoader, write_case: WriteCase
    ) -> None:
        path = write_case(VALID + "bundle: [plugins/present, plugins/absent]\nfixture: seed.sh\n")
        (path.parent / "plugins" / "present").mkdir(parents=True)

        assert issues_of(loader, path) == [
            f"{path}:8: bundle plugins/absent: no such directory next to the case",
            f"{path}:9: fixture seed.sh: no such file next to the case",
        ]

    def test_existing_bundle_and_fixture_load(
        self, loader: CaseLoader, write_case: WriteCase
    ) -> None:
        path = write_case(VALID + "bundle: [plugin]\nfixture: seed.sh\n")
        (path.parent / "plugin").mkdir()
        (path.parent / "seed.sh").write_text("touch seeded\n")

        assert loader.load(path).bundle == ["plugin"]
