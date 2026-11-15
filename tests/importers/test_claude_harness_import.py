import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mozg_eval.cli import app
from mozg_eval.importers.claude_harness import ClaudeHarnessImporter, ClaudePart
from mozg_eval.spec.harness import HarnessLoader

SECRET = "sk-do-not-copy"
SETTINGS = {
    "env": {"API_KEY": SECRET},
    "model": "opus",
    "hooks": {
        "Stop": [
            {
                "hooks": [
                    {"type": "command", "command": "bash $HOME/.claude/hooks/lint.sh --fast"},
                    {"type": "command", "command": "~/.claude/hooks/gone.sh"},
                ]
            }
        ]
    },
}


@pytest.fixture
def config(tmp_path: Path) -> Path:
    """A Claude config dir under a fake HOME, with files that must never be copied."""
    config = tmp_path / "home" / ".claude"
    files = {
        "CLAUDE.md": "Be direct.\n",
        "rules/git.md": "Short commits.\n",
        "rules/lang/python.md": "Type everything.\n",
        "skills/notes/SKILL.md": "---\nname: notes\ndescription: d\n---\n",
        "skills/notes/.cache/x": "junk",
        "skills/synced/readme.txt": "no skill here",
        "agents/reviewer.md": "---\nname: reviewer\ndescription: Reviews.\n---\nReview.\n",
        "agents/draft.md": "---\nname: draft\n---\nNo description.\n",
        "hooks/lint.sh": "echo lint\n",
        "settings.json": json.dumps(SETTINGS),
        ".credentials.json": SECRET,
        "history.jsonl": SECRET,
        "projects/p/memory.md": SECRET,
    }
    for rel, text in files.items():
        (config / rel).parent.mkdir(parents=True, exist_ok=True)
        (config / rel).write_text(text)
    return config


@pytest.fixture
def importer(config: Path, tmp_path: Path) -> ClaudeHarnessImporter:
    return ClaudeHarnessImporter(config, tmp_path / "home")


class TestImport:
    def test_copies_the_allowlist_and_nothing_secret(
        self, importer: ClaudeHarnessImporter, tmp_path: Path
    ) -> None:
        dest = tmp_path / "out"

        summary = importer.run(dest, "mine", list(ClaudePart))

        harness = HarnessLoader().load(dest)
        assert [p.name for p in harness.instructions] == ["CLAUDE.md", "git.md", "lang-python.md"]
        assert [p.name for p in harness.skills] == ["notes"]
        assert [p.name for p in harness.agents] == ["reviewer.md"]
        assert summary.copied == {"instructions": 3, "skills": 1, "agents": 1, "hooks": 2}
        files = [p for p in dest.rglob("*") if p.is_file()]
        assert not any(SECRET in p.read_text() for p in files)
        assert not any(p.name.startswith(".") for p in files)

    def test_hook_scripts_move_into_the_harness(
        self, importer: ClaudeHarnessImporter, tmp_path: Path
    ) -> None:
        dest = tmp_path / "out"

        summary = importer.run(dest, "mine", [ClaudePart.hooks])

        handlers = json.loads((dest / "hooks.json").read_text())["hooks"]["Stop"][0]["hooks"]
        assert [h["command"] for h in handlers] == [
            "bash ${HARNESS_ROOT}/scripts/hooks/lint.sh --fast",
            "~/.claude/hooks/gone.sh",
        ]
        assert (dest / "scripts" / "hooks" / "lint.sh").read_text() == "echo lint\n"
        assert summary.warnings == [
            "hook names ~/.claude/hooks/gone.sh, which isn't a file; left as is"
        ]

    def test_skipped_entries_are_reported(
        self, importer: ClaudeHarnessImporter, config: Path, tmp_path: Path
    ) -> None:
        summary = importer.run(tmp_path / "out", "mine", [ClaudePart.skills, ClaudePart.agents])

        assert summary.warnings == [
            f"{config / 'skills' / 'synced'}: skipped, no SKILL.md",
            f"{config / 'agents' / 'draft.md'}: skipped, frontmatter needs name and description",
        ]

    def test_existing_harness_is_never_overwritten(
        self, importer: ClaudeHarnessImporter, tmp_path: Path
    ) -> None:
        (tmp_path / "out").mkdir()
        (tmp_path / "out" / "keep.md").write_text("mine")

        with pytest.raises(FileExistsError, match="is not empty"):
            importer.run(tmp_path / "out", "mine", list(ClaudePart))


class TestCommand:
    def test_imports_the_parts_asked_for(self, config: Path, tmp_path: Path) -> None:
        argv = ["harness", "import", "claude", str(tmp_path / "rules"), "--source", str(config)]

        result = CliRunner().invoke(app, [*argv, "--part", "instructions"])

        assert result.exit_code == 0, result.output
        assert f"harness rules at {tmp_path / 'rules'}: 3 instructions" in result.output
        assert not (tmp_path / "rules" / "agents").exists()

    def test_non_empty_destination_exits_2(self, config: Path, tmp_path: Path) -> None:
        (tmp_path / "rules").mkdir()
        (tmp_path / "rules" / "x").write_text("")
        argv = ["harness", "import", "claude", str(tmp_path / "rules"), "--source", str(config)]

        result = CliRunner().invoke(app, argv)

        assert result.exit_code == 2
        assert "is not empty" in result.output
