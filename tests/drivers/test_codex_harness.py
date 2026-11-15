import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from mozg_eval.drivers.codex_harness import CodexHarness
from mozg_eval.drivers.codex_plugins import HOOK_COMMANDS
from mozg_eval.spec.harness import HARNESS_FILENAME, HarnessLoader

MANIFEST = """\
schema: mzeval-harness/1
name: rules
mcp:
  docs: {command: npx, args: [docs-mcp], env: {TOKEN: '${DOCS_TOKEN}'}}
  search: {url: 'https://mcp.example.com', headers: {Authorization: 'Bearer ${SEARCH_KEY}'}}
"""
HOOKS = {
    "hooks": {
        "PostToolUse": [
            {
                "matcher": "Write|Edit",
                "hooks": [{"type": "command", "command": "bash ${HARNESS_ROOT}/scripts/nudge.sh"}],
            }
        ]
    }
}
BASE_CONFIG = (
    'project_doc_fallback_filenames = ["CLAUDE.md"]\n\n[skills.bundled]\nenabled = false\n'
)
NATIVE_CONFIG = 'model = "gpt-6.1-sol"\n\n[mcp_servers.native]\ncommand = "x"\n'
ENV = {"DOCS_TOKEN": "t0k", "SEARCH_KEY": "s3c"}


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A trial HOME after the driver prepared it, with a harness written into it."""
    root = tmp_path / "harness"
    files = {
        HARNESS_FILENAME: MANIFEST,
        "instructions/10-git.md": "---\npaths: ['*.py']\n---\nKeep commits short.\n",
        "instructions/20-style.md": "Comments say why.\n",
        "skills/changelog/SKILL.md": "---\nname: changelog\ndescription: d\n---\n",
        "agents/reviewer.md": "---\nname: reviewer\ndescription: Reviews.\n---\nReview diffs.\n",
        "hooks.json": json.dumps(HOOKS),
        "scripts/nudge.sh": "echo nudge\n",
        "native/codex/AGENTS.md": "Native first.\n",
        "native/codex/config.toml": NATIVE_CONFIG,
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(BASE_CONFIG)
    CodexHarness(HarnessLoader().load(root)).materialize(home, tmp_path / "ws", ENV)
    return home


class TestMaterialize:
    def test_instructions_follow_native_agents_md_in_file_order(self, home: Path) -> None:
        text = (home / ".codex" / "AGENTS.md").read_text()

        assert text == "Native first.\n\nKeep commits short.\n\nComments say why.\n"

    def test_skills_and_agents_land_where_codex_reads_user_ones(self, home: Path) -> None:
        agent = tomllib.loads((home / ".codex" / "agents" / "reviewer.toml").read_text())

        assert (home / ".agents" / "skills" / "changelog" / "SKILL.md").is_file()
        assert agent == {
            "name": "reviewer",
            "description": "Reviews.",
            "developer_instructions": "Review diffs.",
        }

    def test_config_keeps_the_drivers_and_natives_keys_and_adds_mcp_and_trust(
        self, home: Path, tmp_path: Path
    ) -> None:
        config = tomllib.loads((home / ".codex" / "config.toml").read_text())

        assert config["skills"] == {"bundled": {"enabled": False}}
        assert config["model"] == "gpt-6.1-sol"
        assert config["mcp_servers"] == {
            "native": {"command": "x"},
            "docs": {"command": "npx", "args": ["docs-mcp"], "env": {"TOKEN": "t0k"}},
            "search": {
                "url": "https://mcp.example.com",
                "http_headers": {"Authorization": "Bearer s3c"},
            },
        }
        ws = str((tmp_path / "ws").resolve())
        assert config["projects"] == {ws: {"trust_level": "trusted"}}

    def test_hooks_run_the_harness_script_through_the_capture(
        self, home: Path, tmp_path: Path
    ) -> None:
        (group,) = json.loads((home / ".codex" / "hooks.json").read_text())["hooks"]["PostToolUse"]
        log = tmp_path / "hooks.jsonl"
        # User hooks get no PLUGIN_ROOT, so the capture must find its commands file itself.
        env = {k: v for k, v in os.environ.items() if k != "PLUGIN_ROOT"}

        result = subprocess.run(
            group["hooks"][0]["command"],
            shell=True,
            input="{}",
            capture_output=True,
            text=True,
            env=env | {"MZEVAL_HOOK_LOG": str(log)},
            check=False,
        )

        assert group["matcher"] == "apply_patch"
        assert (result.returncode, result.stdout) == (0, "nudge\n")
        assert json.loads(log.read_text())["event"] == "PostToolUse"
        assert (home / ".codex" / HOOK_COMMANDS).is_file()
