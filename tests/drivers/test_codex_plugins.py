import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mozg_eval.drivers.claude_plugins import PluginRenderError
from mozg_eval.drivers.codex_plugins import CAPTURE, CodexMarketplace


@pytest.fixture
def market(tmp_path: Path) -> CodexMarketplace:
    return CodexMarketplace(tmp_path / "market", tmp_path / "home" / ".codex")


def hooks_of(plugin: Path) -> dict[str, list[dict[str, object]]]:
    config: dict[str, list[dict[str, object]]] = json.loads(
        (plugin / "hooks" / "hooks.json").read_text()
    )["hooks"]
    return config


class TestRender:
    def test_plugin_gets_a_codex_manifest_and_a_marketplace_entry(
        self, market: CodexMarketplace, claude_plugin: Path, tmp_path: Path
    ) -> None:
        name = market.render(claude_plugin)
        market.write_index()
        plugin = tmp_path / "market" / "plugins" / "notes"

        assert name == "notes"
        assert json.loads((plugin / ".codex-plugin" / "plugin.json").read_text()) == {
            "name": "notes",
            "version": "1.2.0",
            "description": "Notes",
        }
        assert not (plugin / ".git").exists()
        index = json.loads(
            (tmp_path / "market" / ".agents" / "plugins" / "marketplace.json").read_text()
        )
        assert index == {
            "name": "mzeval",
            "plugins": [
                {"name": "notes", "source": {"source": "local", "path": "./plugins/notes"}}
            ],
        }

    def test_skill_text_points_at_the_installed_copy(
        self, market: CodexMarketplace, claude_plugin: Path, tmp_path: Path
    ) -> None:
        market.render(claude_plugin)
        skill = tmp_path / "market" / "plugins" / "notes" / "skills" / "note" / "SKILL.md"
        installed = (
            tmp_path / "home" / ".codex" / "plugins" / "cache" / "mzeval" / "notes" / "1.2.0"
        )

        assert f"Run {installed}/scripts/save.sh" in skill.read_text()
        assert "CLAUDE_PLUGIN_ROOT" in (claude_plugin / "skills" / "note" / "SKILL.md").read_text()

    def test_hooks_run_through_the_capture_and_keep_their_commands(
        self, market: CodexMarketplace, claude_plugin: Path, tmp_path: Path
    ) -> None:
        market.render(claude_plugin)
        plugin = tmp_path / "market" / "plugins" / "notes"
        hooks = hooks_of(plugin)
        post = hooks["PostToolUse"][0]

        assert post["matcher"] == "apply_patch|Bash"
        capture = f'"{sys.executable}" "{CAPTURE}" "notes" "PostToolUse" "PostToolUse:0:1"'
        assert post["hooks"] == [{"type": "command", "command": capture}]
        originals = json.loads((plugin / "hooks" / ".mzeval-hook-commands.json").read_text())
        assert originals == {"SessionStart:0:0": "echo started", "PostToolUse:0:1": "exit 3"}

    def test_install_registers_the_marketplace_then_each_plugin(
        self, market: CodexMarketplace, claude_plugin: Path, tmp_path: Path
    ) -> None:
        market.render(claude_plugin)

        assert market.install_commands("codex") == [
            ["codex", "plugin", "marketplace", "add", str(tmp_path / "market")],
            ["codex", "plugin", "add", "notes@mzeval"],
        ]

    def test_missing_manifest_is_a_render_error(
        self, market: CodexMarketplace, tmp_path: Path
    ) -> None:
        with pytest.raises(PluginRenderError, match=r"no \.claude-plugin/plugin\.json"):
            market.render(tmp_path)

    def test_name_that_is_not_a_plain_directory_name_is_rejected(
        self, market: CodexMarketplace, claude_plugin: Path
    ) -> None:
        (claude_plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "../escape"}')

        with pytest.raises(PluginRenderError, match=r"plugin\.json"):
            market.render(claude_plugin)

    def test_two_plugins_with_one_name_are_rejected(
        self, market: CodexMarketplace, claude_plugin: Path
    ) -> None:
        market.render(claude_plugin)

        with pytest.raises(PluginRenderError, match="already in this trial"):
            market.render(claude_plugin)


class TestCapture:
    def run_hook(self, plugin: Path, log: Path, key: str) -> subprocess.CompletedProcess[str]:
        env = os.environ | {"PLUGIN_ROOT": str(plugin), "MZEVAL_HOOK_LOG": str(log)}
        argv = [sys.executable, str(CAPTURE), "notes", key.split(":")[0], key]
        return subprocess.run(
            argv,
            input='{"hook_event_name": "x"}',
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_runs_the_original_and_logs_what_it_saw(
        self, market: CodexMarketplace, claude_plugin: Path, tmp_path: Path
    ) -> None:
        market.render(claude_plugin)
        log = tmp_path / "hooks.jsonl"

        result = self.run_hook(tmp_path / "market" / "plugins" / "notes", log, "SessionStart:0:0")

        assert (result.returncode, result.stdout) == (0, "started\n")
        record = json.loads(log.read_text())
        assert record["event"] == "SessionStart"
        assert record["stdin"] == '{"hook_event_name": "x"}'
        assert record["stdout"] == "started\n"

    def test_exit_code_passes_through(
        self, market: CodexMarketplace, claude_plugin: Path, tmp_path: Path
    ) -> None:
        market.render(claude_plugin)

        result = self.run_hook(
            tmp_path / "market" / "plugins" / "notes", tmp_path / "hooks.jsonl", "PostToolUse:0:1"
        )

        assert result.returncode == 3
