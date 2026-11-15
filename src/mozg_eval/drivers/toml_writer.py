"""Writes the TOML subset agent configs use, so config.toml can be read, merged and rewritten."""

import json
import re
from collections.abc import Mapping

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")

# tomllib's output types, minus dates, which no agent config this writer touches holds.
type TomlValue = str | int | float | bool | list[TomlValue] | dict[str, TomlValue]


def _key(key: str) -> str:
    return key if _BARE_KEY.match(key) else json.dumps(key)


def _value(value: TomlValue) -> str:
    match value:
        case bool():
            return "true" if value else "false"
        case int() | float():
            return repr(value)
        case str():
            # A JSON string is a valid TOML basic string: same quotes and escapes.
            return json.dumps(value)
        case list():
            return "[" + ", ".join(map(_value, value)) + "]"
        case dict():
            return "{" + ", ".join(f"{_key(k)} = {_value(v)}" for k, v in value.items()) + "}"
    raise TypeError(f"no TOML form for {value!r}")


def merge(base: dict[str, TomlValue], extra: Mapping[str, TomlValue]) -> dict[str, TomlValue]:
    """Tables merge key by key; any other value in `extra` replaces the one in `base`."""
    for key, value in extra.items():
        current = base.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merge(current, value)
        else:
            base[key] = value
    return base


def dumps(data: Mapping[str, TomlValue]) -> str:
    """Scalars and lists first, then each table under its dotted header."""
    lines: list[str] = []

    def table(prefix: str, body: Mapping[str, TomlValue]) -> None:
        scalars = {k: v for k, v in body.items() if not isinstance(v, dict)}
        tables = {k: v for k, v in body.items() if isinstance(v, dict)}
        if prefix and (scalars or not tables):
            lines.append(f"[{prefix}]")
        lines.extend(f"{_key(k)} = {_value(v)}" for k, v in scalars.items())
        if scalars or (prefix and not tables):
            lines.append("")
        for key, sub in tables.items():
            assert isinstance(sub, dict)
            table(f"{prefix}.{_key(key)}" if prefix else _key(key), sub)

    table("", data)
    return "\n".join(lines).rstrip() + "\n"
