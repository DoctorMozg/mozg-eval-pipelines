import json
from pathlib import Path
from typing import Any

import pytest

from mozg_eval.drivers.opencode_harness import OpenCodeHarness, merge_json
from mozg_eval.spec.harness import HARNESS_FILENAME, HarnessLoader, read_frontmatter

MANIFEST = """\
schema: mzeval-harness/1
name: rules
mcp:
  docs: {command: npx, args: [docs-mcp], env: {TOKEN: '${DOCS_TOKEN}'}}
  search: {url: 'https://mcp.example.com', headers: {Authorization: 'Bearer ${SEARCH_KEY}'}}
"""
AGENT = "---\nname: reviewer\ndescription: Reviews.\ntools: Read, Grep\n---\nReview diffs.\n"
NATIVE = {"model": "openrouter/x", "permissions": [{"action": "bash", "effect": "deny"}]}
ENV = {"DOCS_TOKEN": "t0k", "SEARCH_KEY": "s3c"}


class TestMergeJson:
    def test_objects_merge_and_lists_append(self) -> None:
        base: dict[str, Any] = {"a": {"x": 1}, "rules": [1], "model": "old"}

        merged = merge_json(base, {"a": {"y": 2}, "rules": [2], "model": "new"})

        assert merged == {"a": {"x": 1, "y": 2}, "rules": [1, 2], "model": "new"}


class TestMaterialize:
    @pytest.fixture
    def config_dir(self, tmp_path: Path) -> Path:
        return tmp_path / "home" / ".config" / "opencode"

    @pytest.fixture
    def config(self, tmp_path: Path, config_dir: Path) -> dict[str, Any]:
        root = tmp_path / "harness"
        files = {
            HARNESS_FILENAME: MANIFEST,
            "instructions/git.md": "Keep commits short.\n",
            "skills/changelog/SKILL.md": "---\nname: changelog\ndescription: d\n---\n",
            "agents/reviewer.md": AGENT,
            "native/opencode/opencode.json": json.dumps(NATIVE),
            "native/opencode/plugins/nudge/index.ts": "export default {}\n",
        }
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        return OpenCodeHarness(HarnessLoader().load(root)).materialize(config_dir, ENV)

    def test_files_land_in_the_global_config_dir(
        self, config: dict[str, Any], config_dir: Path
    ) -> None:
        assert (config_dir / "AGENTS.md").read_text() == "Keep commits short.\n"
        assert (config_dir / "skills" / "changelog" / "SKILL.md").is_file()
        assert (config_dir / "plugins" / "nudge" / "index.ts").is_file()
        assert not (config_dir / "opencode.json").exists()

    def test_agent_becomes_a_subagent_without_claude_keys(
        self, config: dict[str, Any], config_dir: Path
    ) -> None:
        meta, body = read_frontmatter(config_dir / "agents" / "reviewer.md")

        assert meta == {"description": "Reviews.", "mode": "subagent"}
        assert body == "Review diffs.\n"

    def test_config_holds_native_keys_and_mcp_servers(self, config: dict[str, Any]) -> None:
        assert config == NATIVE | {
            "mcp": {
                "servers": {
                    "docs": {
                        "type": "local",
                        "command": ["npx", "docs-mcp"],
                        "environment": {"TOKEN": "t0k"},
                    },
                    "search": {
                        "type": "remote",
                        "url": "https://mcp.example.com",
                        "headers": {"Authorization": "Bearer s3c"},
                        "oauth": False,
                    },
                }
            }
        }
