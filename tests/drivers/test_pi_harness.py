import json
from pathlib import Path

import pytest

from mozg_eval.drivers.pi_harness import PiHarness
from mozg_eval.spec.harness import HARNESS_FILENAME, HarnessLoader

MANIFEST = """\
schema: mzeval-harness/1
name: rules
mcp:
  docs: {command: npx, args: [docs-mcp], env: {TOKEN: '${DOCS_TOKEN}'}}
  search: {url: 'https://mcp.example.com', headers: {Authorization: 'Bearer ${SEARCH_KEY}'}}
"""
NATIVE_MCP = {"mcpServers": {"native": {"command": "x"}}}
ENV = {"DOCS_TOKEN": "t0k", "SEARCH_KEY": "s3c"}


def harness_at(root: Path, files: dict[str, str]) -> PiHarness:
    for rel, text in (
        {HARNESS_FILENAME: "schema: mzeval-harness/1\nname: rules\n"} | files
    ).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return PiHarness(HarnessLoader().load(root))


class TestMaterialize:
    @pytest.fixture
    def agent_dir(self, tmp_path: Path) -> Path:
        return tmp_path / "home" / ".pi" / "agent"

    @pytest.fixture
    def flags(self, tmp_path: Path, agent_dir: Path) -> list[str]:
        harness = harness_at(
            tmp_path / "harness",
            {
                HARNESS_FILENAME: MANIFEST,
                "instructions/10-git.md": "Keep commits short.\n",
                "instructions/20-style.md": "---\npaths: ['*.py']\n---\nComments say why.\n",
                "skills/changelog/SKILL.md": "---\nname: changelog\ndescription: d\n---\n",
                "native/pi/AGENTS.md": "Native first.\n",
                "native/pi/mcp.json": json.dumps(NATIVE_MCP),
                "native/pi/extensions/b-guard.ts": "export default () => {}\n",
                "native/pi/extensions/a-nudge/index.ts": "export default () => {}\n",
            },
        )
        return harness.materialize(agent_dir, ENV)

    def test_flags_load_skills_each_extension_and_builtin_mcp(
        self, flags: list[str], agent_dir: Path
    ) -> None:
        extensions = agent_dir / "extensions"

        assert flags == [
            *("--skill", str(agent_dir / "skills")),
            *("-e", str(extensions / "a-nudge")),
            *("-e", str(extensions / "b-guard.ts")),
            *("-e", "builtin:mcp"),
        ]

    def test_files_land_in_the_agent_dir(self, flags: list[str], agent_dir: Path) -> None:
        text = (agent_dir / "AGENTS.md").read_text()

        assert text == "Native first.\n\nKeep commits short.\n\nComments say why.\n"
        assert (agent_dir / "skills" / "changelog" / "SKILL.md").is_file()

    def test_mcp_json_holds_native_then_manifest_servers(
        self, flags: list[str], agent_dir: Path
    ) -> None:
        assert json.loads((agent_dir / "mcp.json").read_text()) == {
            "mcpServers": {
                "native": {"command": "x"},
                "docs": {"command": "npx", "args": ["docs-mcp"], "env": {"TOKEN": "t0k"}},
                "search": {
                    "url": "https://mcp.example.com",
                    "headers": {"Authorization": "Bearer s3c"},
                },
            }
        }

    def test_instructions_alone_need_no_flags(self, tmp_path: Path, agent_dir: Path) -> None:
        harness = harness_at(tmp_path / "plain", {"instructions/a.md": "Be brief.\n"})

        assert harness.materialize(agent_dir, {}) == []
        assert (agent_dir / "AGENTS.md").read_text() == "Be brief.\n"
