import json
from pathlib import Path

import pytest

from mozg_eval.drivers.claude_harness import ClaudeHarness, expand_env
from mozg_eval.drivers.protocol import AgentError
from mozg_eval.spec.harness import HARNESS_FILENAME, Harness, HarnessLoader

MANIFEST = """\
schema: mzeval-harness/1
name: rules-v2
mcp:
  docs: {command: npx, args: [docs-mcp], env: {TOKEN: '${DOCS_TOKEN}'}}
  search: {url: 'https://mcp.example.com', headers: {Authorization: 'Bearer ${SEARCH_KEY}'}}
"""
HOOKS = {
    "hooks": {
        "UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "${HARNESS_ROOT}/scripts/nudge.sh"}]}
        ]
    }
}
NATIVE_SETTINGS = {
    "outputStyle": "terse",
    "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "true"}]}]},
}
ENV = {"DOCS_TOKEN": "t0k", "SEARCH_KEY": "s3c"}


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    root = tmp_path / "harness"
    write(
        root,
        {
            HARNESS_FILENAME: MANIFEST,
            "instructions/git.md": "Keep commit messages short.\n",
            "skills/changelog/SKILL.md": "---\nname: changelog\ndescription: d\n---\n",
            "agents/reviewer.md": "---\nname: reviewer\ndescription: Reviews.\n---\nReview.\n",
            "hooks.json": json.dumps(HOOKS),
            "scripts/nudge.sh": "echo nudge\n",
            "native/claude/settings.json": json.dumps(NATIVE_SETTINGS),
        },
    )
    return HarnessLoader().load(root)


class TestMaterialize:
    @pytest.fixture
    def config(self, harness: Harness, tmp_path: Path) -> Path:
        config = tmp_path / "home" / ".claude"
        config.mkdir(parents=True)
        ClaudeHarness(harness).materialize(config, tmp_path / "ws", ENV)
        return config

    def test_files_land_where_claude_reads_user_config(self, config: Path) -> None:
        assert (config / "rules" / "git.md").read_text() == "Keep commit messages short.\n"
        assert (config / "skills" / "changelog" / "SKILL.md").is_file()
        assert (config / "agents" / "reviewer.md").is_file()

    def test_hooks_merge_into_native_settings_with_the_root_filled(
        self, config: Path, harness: Harness
    ) -> None:
        settings = json.loads((config / "settings.json").read_text())

        assert settings["outputStyle"] == "terse"
        commands = [m["hooks"][0]["command"] for m in settings["hooks"]["UserPromptSubmit"]]
        assert commands == ["true", f"{harness.root.resolve()}/scripts/nudge.sh"]

    def test_mcp_servers_and_trust_go_into_claude_json(self, config: Path, tmp_path: Path) -> None:
        data = json.loads((config / ".claude.json").read_text())

        assert data["mcpServers"] == {
            "docs": {
                "type": "stdio",
                "command": "npx",
                "args": ["docs-mcp"],
                "env": {"TOKEN": "t0k"},
            },
            "search": {
                "type": "http",
                "url": "https://mcp.example.com",
                "headers": {"Authorization": "Bearer s3c"},
            },
        }
        assert data["projects"][str((tmp_path / "ws").resolve())] == {
            "hasTrustDialogAccepted": True
        }


class TestEnvRefs:
    def test_unset_variable_stops_the_trial_with_its_name(self) -> None:
        with pytest.raises(AgentError, match=r"\$\{DOCS_TOKEN\}"):
            expand_env("${DOCS_TOKEN}", {})

    def test_text_without_refs_is_unchanged(self) -> None:
        assert expand_env("plain $HOME", {}) == "plain $HOME"


class TestMissing:
    def test_complete_init_misses_nothing(self, harness: Harness) -> None:
        init = {
            "skills": ["changelog", "simplify"],
            "agents": ["reviewer", "Explore"],
            "mcp_servers": [
                {"name": "docs", "status": "connected"},
                {"name": "search", "status": "connected"},
            ],
        }

        assert ClaudeHarness(harness).missing(init) == []

    def test_each_absent_part_is_named(self, harness: Harness) -> None:
        init = {"skills": [], "agents": [], "mcp_servers": [{"name": "docs", "status": "failed"}]}

        assert ClaudeHarness(harness).missing(init) == [
            "skill changelog",
            "subagent reviewer",
            "MCP server docs (failed)",
            "MCP server search (absent)",
        ]
