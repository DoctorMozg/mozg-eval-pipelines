import pytest

from mozg_eval.importers.claude_wire import ignoring_case, rewrite_trace, rewrite_write_input

STATE = "\\.mz/task/\\d{4}_\\d{2}_\\d{2}_audit_[a-z0-9_]+/state\\.md"


class TestTraceShapes:
    @pytest.mark.parametrize(
        ("pattern", "grader"),
        [
            (
                '"subtype":"hook_response"[^\\n]*Advice[^\\\\]*\\\\+n- [I-0001]',
                {"type": "hook_fired", "match": "Advice[^\\\\]*\\\\+n- [I-0001]"},
            ),
            (
                '"subtype":"hook_response"[^\\n]*"hook_event":"SessionStart"[^\\n]*OPAL-19',
                {"type": "hook_fired", "event": "SessionStart", "match": "OPAL-19"},
            ),
            (
                '"subtype":"hook_response"[^\\n]*"hook_event":"UserPromptSubmit"',
                {"type": "hook_fired", "event": "UserPromptSubmit"},
            ),
            (
                '"type":"tool_result"[^\\n]*kestrel-east',
                {"type": "regex", "target": "tool_results", "pattern": "kestrel-east"},
            ),
            (
                '"type":"text","text":"[^\\n]*Panel ready[^\\n]*Not selected:',
                {
                    "type": "regex",
                    "target": "replies",
                    "pattern": "^[\\s\\S]*Panel ready[\\s\\S]*Not selected:",
                },
            ),
            (
                '"role":"assistant","content":\\[\\{"type":"text","text":"(?=[^\\n]*\\bmetric\\b)[^\\n]*"parent_tool_use_id":null',
                {"type": "regex", "target": "replies", "pattern": "^(?=[\\s\\S]*\\bmetric\\b)"},
            ),
            (
                '"type"\\s*:\\s*"text"\\s*,\\s*"text"\\s*:\\s*"'
                '(?:[^"\\\\]|\\\\.)*?ready for review',
                {"type": "regex", "target": "replies", "pattern": "^[\\s\\S]*?ready for review"},
            ),
            (
                f"File created successfully at: \\S*{STATE}",
                {"type": "file_changed", "path_match": STATE + "$"},
            ),
            (
                '"name":"(?:Write|Edit)","input":\\{[^\\n]*awaiting approval: panel gate',
                {"type": "file_changed", "content_match": "awaiting approval: panel gate"},
            ),
            (
                '"name":"Write"[^\\n]*\\.mz/handoff/[^\\n]*## Done[^\\n]*?Evidence:',
                {
                    "type": "file_changed",
                    "path_match": "\\.mz/handoff/",
                    "content_match": "## Done[\\s\\S]*?Evidence:",
                },
            ),
            (
                '"name":"(?:Write|Edit)"[^\\n]*\\.mz/task/x_[^\\n]*state\\.md',
                {"type": "file_changed", "path_match": "\\.mz/task/x_.*state\\.md"},
            ),
            (
                "^(?=.*_audit_[a-z]+/(?:findings|summary)\\.md)(?=.*repo\\.py:\\d+)",
                {
                    "type": "file_changed",
                    "path_match": "_audit_[a-z]+/(?:findings|summary)\\.md",
                    "content_match": "^(?=[\\s\\S]*repo\\.py:\\d+)",
                },
            ),
            (
                "\\.mz/task/\\d{4}_\\d{2}_\\d{2}_naturalize_",
                {
                    "type": "file_changed",
                    "path_match": "\\.mz/task/\\d{4}_\\d{2}_\\d{2}_naturalize_",
                },
            ),
            (
                "_audit_[a-z0-9_]+/state\\.md[^\\n]*Status: complete",
                {
                    "type": "file_changed",
                    "path_match": "_audit_[a-z0-9_]+/state\\.md",
                    "content_match": "Status: complete",
                },
            ),
        ],
    )
    def test_known_shape_becomes_a_portable_grader(
        self, pattern: str, grader: dict[str, str]
    ) -> None:
        rewrite = rewrite_trace(pattern)

        assert rewrite is not None
        assert rewrite.graders == [grader]

    @pytest.mark.parametrize(
        "pattern",
        [
            "orphans:(?:(?!broken_links:)[\\s\\S]){0,600}Lonely orphan",
            '"type":"tool_use"[^\\n]*a\\.sql[^\\n]*db/MIG-',
            "\\[\\[Sourdough",
        ],
    )
    def test_unclear_intent_stays_claude_only(self, pattern: str) -> None:
        assert rewrite_trace(pattern) is None

    def test_ignoring_case_leaves_paths_alone(self) -> None:
        rewrite = rewrite_trace("_x/state\\.md[^\\n]*done")
        assert rewrite is not None

        assert ignoring_case(rewrite).graders == [
            {"type": "file_changed", "path_match": "_x/state\\.md", "content_match": "(?i)done"}
        ]


class TestWriteInputShapes:
    @pytest.mark.parametrize(
        ("pattern", "absent", "graders"),
        [
            ('"file_path"', True, [{"type": "file_changed"}]),
            (
                '^(?=[\\s\\S]*"file_path"\\s*:\\s*"[^"]*/panel\\.md")(?=[\\s\\S]*lens-)',
                False,
                [
                    {
                        "type": "file_changed",
                        "path_match": '[^"]*/panel\\.md$',
                        "content_match": "^(?=[\\s\\S]*lens-)",
                    }
                ],
            ),
            (
                "^(?=[\\s\\S]*brief\\.json)(?=[\\s\\S]*Xero)(?=[\\s\\S]*\\\\n## Leeds)",
                False,
                [
                    {
                        "type": "file_changed",
                        "path_match": "brief\\.json",
                        "content_match": "^(?=[\\s\\S]*Xero)(?=[\\s\\S]*\\n## Leeds)",
                    }
                ],
            ),
            (
                "notes/ethics\\.md",
                False,
                [{"type": "file_changed", "path_match": "notes/ethics\\.md"}],
            ),
            (
                "awaiting approval: brief gate",
                False,
                [{"type": "file_changed", "content_match": "awaiting approval: brief gate"}],
            ),
        ],
    )
    def test_known_shape_becomes_file_checks(
        self, pattern: str, absent: bool, graders: list[dict[str, str]]
    ) -> None:
        rewrite = rewrite_write_input(pattern, absent)

        assert rewrite is not None
        assert rewrite.graders == graders

    def test_backreference_across_lookaheads_stays_a_tool_check(self) -> None:
        pattern = '^(?=[\\s\\S]*"file_path"\\s*:\\s*"a/(\\d+)\\.md")(?=[\\s\\S]*\\1)'

        assert rewrite_write_input(pattern, absent=False) is None
