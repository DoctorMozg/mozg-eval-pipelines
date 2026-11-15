"""Rewrites 1.1 regexes over Claude's stream-json into graders that hold on every driver.

Only shapes whose intent is unambiguous are rewritten; anything else stays Claude-only.
"""

import re
from dataclasses import dataclass, field
from typing import Any

_HOOK = '"subtype":"hook_response"[^\\n]*'
# Claude writes hook_event before the hook's stdout, so a pattern can name the event first.
_HOOK_EVENT = re.compile(r'"hook_event":"(?P<event>\w+)"(?:\[\^\\n\]\*)?')
_TOOL_RESULT = '"type":"tool_result"[^\\n]*'
_REPLY_PREFIXES = (
    '"role":"assistant","content":\\[\\{"type":"text","text":"',
    '"type":"text","text":"',
    '"type"\\s*:\\s*"text"\\s*,\\s*"text"\\s*:\\s*"',
)
_REPLY_SUFFIX = re.compile(r'(?:\[\^\\n\]\*)?"parent_tool_use_id":null$')
_FILE_CREATED = "File created successfully at: \\S*"
# Openers of a regex over a Write/Edit tool_use line; what follows is a path, content, or both.
_WRITE_PREFIXES = (
    '^(?=[^\\n]*"name":"(?:Write|Edit)")',
    '"name":"(?:Write|Edit)","input":\\{',
    '"name":"(?:Write|Bash)","input":\\{',
    '"name"\\s*:\\s*"(?:Write|Edit)"',
    '"name":"(?:Write|Edit)"',
    '"name":"Write"',
)
_ANY_LINE = "[^\\n]*"
_LOOKAHEAD_OPENERS = ("^(?=[\\s\\S]*", "^(?=.*")
_FILE_PATH_KEY = '"file_path"\\s*:\\s*"'
_PATH_LIKE = re.compile(r"/|\\\.(?:md|json|sql|txt|ya?ml)\b")
_BACKREF = re.compile(r"\\[1-9]")


@dataclass
class Rewrite:
    rule: str
    graders: list[dict[str, Any]] = field(default_factory=list)


def _first_lookahead(pattern: str) -> tuple[str, str] | None:
    """Split `^(?=.*X)REST` into X and REST, minding escapes, classes and nested groups."""
    opener = next((o for o in _LOOKAHEAD_OPENERS if pattern.startswith(o)), None)
    if opener is None:
        return None
    depth, in_class, i = 1, False, len(opener)
    while i < len(pattern):
        char = pattern[i]
        if char == "\\":
            i += 2
            continue
        if in_class:
            in_class = char != "]"
        elif char == "[":
            in_class = True
        elif char in "()":
            depth += 1 if char == "(" else -1
            if depth == 0:
                return pattern[len(opener) : i], pattern[i + 1 :]
        i += 1
    return None


def _unjson(body: str) -> str:
    """A regex over JSON-escaped text, made to read the decoded text instead."""
    body = body.replace('(?:[^"\\\\]|\\\\.)', "[\\s\\S]").replace("[^\\n]", "[\\s\\S]")
    body = body.replace("(?=.*", "(?=[\\s\\S]*")
    return body.replace("\\\\+n", "\\n").replace("\\\\n", "\\n")


def _path_like(part: str) -> bool:
    return '"' not in part and bool(_PATH_LIKE.search(part))


def _changed(path: str | None, content: str | None) -> dict[str, Any]:
    grader: dict[str, Any] = {"type": "file_changed"}
    if path is not None:
        grader["path_match"] = path
    if content is not None:
        grader["content_match"] = content
    return grader


def _path_and_content(rest: str) -> dict[str, Any] | None:
    """`<path>[^\\n]*<content>`, `<path>` or `<content>` from a written file's line."""
    head, sep, tail = rest.partition(_ANY_LINE)
    if sep and _path_like(head) and _path_like(tail) and _ANY_LINE not in tail:
        return _changed(f"{head}.*{tail}", None)
    if sep and _path_like(head) and tail:
        return _changed(head, _unjson(tail))
    if not sep and _path_like(rest):
        return _changed(rest, None)
    if not sep and '"' not in rest:
        return _changed(None, _unjson(rest))
    return None


def rewrite_trace(pattern: str) -> Rewrite | None:
    """A 1.1 `regex target: trace` as portable graders, or None to keep it Claude-only."""
    if pattern.startswith(_HOOK):
        # Hook stdout keeps its own JSON escaping, which `\\+n`-style patterns already allow for.
        rest = pattern.removeprefix(_HOOK)
        hook: dict[str, Any] = {"type": "hook_fired"}
        if named := _HOOK_EVENT.match(rest):
            hook["event"] = named["event"]
            rest = rest[named.end() :]
        if rest:
            hook["match"] = rest
        return Rewrite("trace hook_response -> hook_fired", [hook])
    if pattern.startswith(_TOOL_RESULT):
        body = _unjson(pattern.removeprefix(_TOOL_RESULT))
        return Rewrite(
            "trace tool_result -> regex tool_results",
            [{"type": "regex", "target": "tool_results", "pattern": body}],
        )
    for prefix in _REPLY_PREFIXES:
        if pattern.startswith(prefix):
            body = _REPLY_SUFFIX.sub("", pattern.removeprefix(prefix))
            return Rewrite(
                "trace text -> regex replies",
                [{"type": "regex", "target": "replies", "pattern": "^" + _unjson(body)}],
            )
    if pattern.startswith(_FILE_CREATED):
        path = pattern.removeprefix(_FILE_CREATED) + "$"
        return Rewrite("trace file created -> file_changed", [_changed(path, None)])
    for prefix in _WRITE_PREFIXES:
        if pattern.startswith(prefix + _ANY_LINE) and (
            grader := _path_and_content(pattern.removeprefix(prefix + _ANY_LINE))
        ):
            return Rewrite("trace Write/Edit -> file_changed", [grader])
    if (lead := _first_lookahead(pattern)) and _path_like(lead[0]) and lead[1]:
        content = "^" + _unjson(lead[1])
        return Rewrite("trace file lookaheads -> file_changed", [_changed(lead[0], content)])
    if pattern.startswith("\\.mz/") and _ANY_LINE not in pattern:
        return Rewrite("trace task path -> file_changed", [_changed(pattern, None)])
    if _ANY_LINE in pattern and '"' not in pattern and (grader := _path_and_content(pattern)):
        return Rewrite("trace path and text -> file_changed", [grader])
    return None


def rewrite_write_input(pattern: str, absent: bool) -> Rewrite | None:
    """A Write/Edit `input_match` as checks on changed files; `absent` is a `max: 0` grader."""
    if _BACKREF.search(pattern):
        return None
    if pattern == '"file_path"':
        return Rewrite("Write any file -> file_changed", [_changed(None, None)])
    if lead := _first_lookahead(pattern):
        path, rest = lead
        if path.startswith(_FILE_PATH_KEY) and path.endswith('"'):
            path = path.removeprefix(_FILE_PATH_KEY).removesuffix('"') + "$"
        elif not _path_like(path):
            return None
        content = "^" + _unjson(rest) if rest else None
        return Rewrite("Write path lookaheads -> file_changed", [_changed(path, content)])
    if pattern.startswith("^") or '"' in pattern:
        return None
    if absent:
        # The 1.1 regex ran over path and content together; no file may match either.
        return Rewrite(
            "Write absent -> file_changed path and content",
            [_changed(None, _unjson(pattern)), _changed(pattern, None) | {"suffix": "-path"}],
        )
    if _path_like(pattern):
        return Rewrite("Write path -> file_changed", [_changed(pattern, None)])
    return Rewrite("Write content -> file_changed", [_changed(None, _unjson(pattern))])


def ignoring_case(rewrite: Rewrite) -> Rewrite:
    """Carry a 1.1 `i` flag over; paths stay case-sensitive, as they are on disk."""
    graders = []
    for grader in rewrite.graders:
        grader = dict(grader)
        if grader["type"] == "regex":
            grader["flags"] = "i"
        for key in ("match", "content_match"):
            if key in grader:
                grader[key] = "(?i)" + grader[key]
        graders.append(grader)
    return Rewrite(rewrite.rule, graders)
