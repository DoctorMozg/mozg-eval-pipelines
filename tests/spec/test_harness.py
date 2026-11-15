from collections.abc import Callable
from pathlib import Path

import pytest

from mozg_eval.spec.harness import (
    HARNESS_FILENAME,
    Harness,
    HarnessLoader,
    HarnessLoadError,
    HttpServer,
    StdioServer,
    read_frontmatter,
)

MANIFEST = """\
schema: mzeval-harness/1
name: rules-v2
"""

AGENT = "---\nname: reviewer\ndescription: Reviews diffs.\n---\nYou review diffs.\n"

type Build = Callable[[dict[str, str]], Path]


@pytest.fixture
def loader() -> HarnessLoader:
    return HarnessLoader()


@pytest.fixture
def build(tmp_path: Path) -> Build:
    """Writes {relative path: text} under a fresh harness folder; a trailing / makes a folder."""

    def write(files: dict[str, str]) -> Path:
        root = tmp_path / "harness"
        for rel, text in ({HARNESS_FILENAME: MANIFEST} | files).items():
            path = root / rel
            if rel.endswith("/"):
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return root

    return write


def issues_of(loader: HarnessLoader, root: Path) -> list[str]:
    with pytest.raises(HarnessLoadError) as caught:
        loader.load(root)
    return [str(issue) for issue in caught.value.issues]


class TestValidHarness:
    @pytest.fixture
    def harness(self, loader: HarnessLoader, build: Build) -> Harness:
        root = build(
            {
                HARNESS_FILENAME: MANIFEST
                + "plugins: [../plugin]\n"
                + "mcp:\n  docs: {command: npx, args: [docs-mcp], env: {TOKEN: x}}\n"
                + "  search: {url: 'https://mcp.example.com', headers: {X-Key: k}}\n",
                "instructions/20-git.md": "Commit messages stay short.\n",
                "instructions/10-style.md": "Comments say why.\n",
                "skills/changelog/SKILL.md": "---\nname: changelog\n---\n",
                "agents/reviewer.md": AGENT,
                "hooks.json": '{"hooks": {"UserPromptSubmit": []}}',
                "native/claude/output-styles/terse.md": "Be terse.\n",
                "README.md": "Notes for people.\n",
                "../plugin/": "",
            }
        )
        return loader.load(root)

    def test_every_part_is_found(self, harness: Harness) -> None:
        assert harness.name == "rules-v2"
        assert harness.parts() == {"instructions", "skills", "agents", "hooks", "mcp", "plugins"}
        assert [p.name for p in harness.instructions] == ["10-style.md", "20-git.md"]
        assert [p.name for p in harness.skills] == ["changelog"]
        assert harness.native.keys() == {"claude"}
        assert harness.plugins == ((harness.root.parent / "plugin").resolve(),)

    def test_mcp_servers_keep_their_kind(self, harness: Harness) -> None:
        servers = harness.manifest.mcp

        assert servers["docs"] == StdioServer(command="npx", args=["docs-mcp"], env={"TOKEN": "x"})
        assert servers["search"] == HttpServer(
            url="https://mcp.example.com", headers={"X-Key": "k"}
        )

    def test_digest_changes_with_any_loaded_file(
        self, harness: Harness, loader: HarnessLoader
    ) -> None:
        assert loader.load(harness.root).digest == harness.digest

        (harness.root.parent / "plugin" / "skill.md").write_text("new")

        assert loader.load(harness.root).digest != harness.digest

    def test_manifest_alone_is_an_empty_harness(self, loader: HarnessLoader, build: Build) -> None:
        assert loader.load(build({})).parts() == frozenset()


class TestErrors:
    def test_missing_manifest(self, loader: HarnessLoader, tmp_path: Path) -> None:
        assert issues_of(loader, tmp_path) == [
            f"{tmp_path / HARNESS_FILENAME}: a harness needs a {HARNESS_FILENAME}"
        ]

    def test_unknown_keys_point_at_their_lines(self, loader: HarnessLoader, build: Build) -> None:
        root = build(
            {HARNESS_FILENAME: MANIFEST + "mcp:\n  docs: {command: x, cwd: /}\nbogus: 1\n"}
        )
        manifest = root / HARNESS_FILENAME

        assert issues_of(loader, root) == [
            f"{manifest}:4: unknown key 'cwd' in mcp.docs",
            f"{manifest}:5: unknown key 'bogus' in <harness>",
        ]

    def test_layout_mistakes_name_the_offending_path(
        self, loader: HarnessLoader, build: Build
    ) -> None:
        root = build(
            {
                "instructions/nested/": "",
                "skills/no-skill-file/notes.md": "",
                "agents/anonymous.md": "---\nname: anonymous\n---\nbody\n",
                "hooks.json": '{"UserPromptSubmit": []}',
                "native/cursor/": "",
                "setting.json": "{}",
            }
        )

        assert issues_of(loader, root) == [
            f"{root / 'setting.json'}: unexpected entry in a harness; it may hold README.md, "
            "agents, hooks.json, instructions, native, skills",
            f"{root / 'instructions/nested'}: instructions are flat .md files",
            f"{root / 'skills/no-skill-file'}: a skill is a folder holding SKILL.md",
            f"{root / 'agents/anonymous.md'}:1: frontmatter lacks description",
            f"{root / 'hooks.json'}:1: hooks.json holds "
            '{"hooks": {"<Event>": [...]}}, as Claude settings do',
            f"{root / 'native/cursor'}: native overlays are folders named "
            "claude, codex, opencode, pi",
        ]

    def test_missing_plugin_is_reported(self, loader: HarnessLoader, build: Build) -> None:
        root = build({HARNESS_FILENAME: MANIFEST + "plugins: [../absent]\n"})

        assert issues_of(loader, root) == [
            f"{root / HARNESS_FILENAME}: plugin ../absent: no such directory next to the harness"
        ]

    def test_http_server_needs_an_http_url(self, loader: HarnessLoader, build: Build) -> None:
        root = build({HARNESS_FILENAME: MANIFEST + "mcp:\n  docs: {url: 'ftp://x'}\n"})

        assert any("String should match pattern" in issue for issue in issues_of(loader, root))


class TestDiscover:
    def test_finds_harness_folders_by_their_manifest(
        self, loader: HarnessLoader, build: Build, tmp_path: Path
    ) -> None:
        root = build({})

        assert loader.discover([tmp_path]) == [root]
        assert loader.discover([root / HARNESS_FILENAME]) == [root]


class TestFrontmatter:
    def test_reads_meta_and_body(self, tmp_path: Path) -> None:
        path = tmp_path / "agent.md"
        path.write_text(AGENT)

        assert read_frontmatter(path) == (
            {"name": "reviewer", "description": "Reviews diffs."},
            "You review diffs.\n",
        )

    def test_file_without_frontmatter_is_all_body(self, tmp_path: Path) -> None:
        path = tmp_path / "plain.md"
        path.write_text("Just text.\n")

        assert read_frontmatter(path) == ({}, "Just text.\n")

    def test_unclosed_frontmatter_is_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.md"
        path.write_text("---\nname: x\n")

        with pytest.raises(ValueError, match="no closing ---"):
            read_frontmatter(path)
