import tomllib

import pytest

from mozg_eval.drivers.toml_writer import TomlValue, dumps, merge

CONFIG: dict[str, TomlValue] = {
    "project_doc_fallback_filenames": ["CLAUDE.md"],
    "model_reasoning_effort": "high",
    "skills": {"bundled": {"enabled": False}},
    "mcp_servers": {
        "docs": {"command": "npx", "args": ["-y", "docs"], "env": {"TOKEN": 'q"uote\n'}},
        "empty": {},
    },
    "projects": {"/tmp/my ws": {"trust_level": "trusted"}},
    "limits": {"retries": 3, "backoff": 1.5, "steps": [{"name": "a"}, {"name": "b"}]},
}


class TestDumps:
    def test_round_trips_through_tomllib(self) -> None:
        assert tomllib.loads(dumps(CONFIG)) == CONFIG

    def test_empty_table_keeps_a_blank_line_before_the_next(self) -> None:
        assert dumps({"a": {"env": {}, "b": {"x": 1}}}) == "[a.env]\n\n[a.b]\nx = 1\n"

    def test_paths_become_quoted_keys(self) -> None:
        assert '[projects."/tmp/my ws"]' in dumps(CONFIG)

    def test_dates_are_refused(self) -> None:
        with pytest.raises(TypeError, match="no TOML form"):
            dumps({"when": object()})  # type: ignore[dict-item]


class TestMerge:
    def test_tables_merge_and_other_values_are_replaced(self) -> None:
        base: dict[str, TomlValue] = {"a": {"x": 1, "y": [1]}, "b": "old"}

        merged = merge(base, {"a": {"y": [2], "z": True}, "b": "new"})

        assert merged == {"a": {"x": 1, "y": [2], "z": True}, "b": "new"}
