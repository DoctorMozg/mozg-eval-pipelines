"""Setting lineage: each resolved execution setting with the layer that set it."""

from collections.abc import Mapping

from pydantic import JsonValue

from mozg_eval.events.models import Setting
from mozg_eval.spec.models import Execution


def resolve_settings(execution: Execution, cli: Mapping[str, JsonValue]) -> dict[str, Setting]:
    """Layer CLI flags over the case's execution block, whose unset fields keep their defaults."""
    values = execution.model_dump(mode="json")
    unknown = cli.keys() - values.keys()
    if unknown:
        raise ValueError(f"unknown execution settings from the CLI: {sorted(unknown)}")
    settings = {
        key: Setting(value=value, source="case" if key in execution.model_fields_set else "default")
        for key, value in values.items()
    }
    for key, value in cli.items():
        settings[key] = Setting(value=value, source="cli")
    return settings
