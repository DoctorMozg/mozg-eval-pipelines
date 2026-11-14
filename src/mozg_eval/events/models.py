"""Canonical events: every connector emits these, and graders read nothing else."""

from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    Field,
    JsonValue,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveInt,
    TypeAdapter,
)

from mozg_eval.spec.models import StrictModel, ToolKind

type Evidence = Literal["native", "inferred"]
type EndStatus = Literal["ok", "error", "cancelled", "timeout"]
type SettingSource = Literal["default", "case", "suite", "config", "cli"]
type ChangeKind = Literal["created", "modified", "deleted"]


class Setting(StrictModel):
    value: JsonValue
    source: SettingSource


class Start(StrictModel):
    type: Literal["start"] = "start"
    driver: str
    driver_version: str
    prompt: str
    workspace: str
    settings: dict[str, Setting] = Field(
        description="each resolved setting and the layer that set it"
    )


class Message(StrictModel):
    type: Literal["message"] = "message"
    role: Literal["user", "assistant"]
    text: str


class ToolCall(StrictModel):
    type: Literal["tool_call"] = "tool_call"
    id: str
    kind: ToolKind | Literal["other"]
    name: str = Field(description="the tool's native name, e.g. Bash or apply_patch")
    input: dict[str, JsonValue] = Field(
        default_factory=dict, description="the tool's arguments as the driver saw them"
    )


class ToolResult(StrictModel):
    type: Literal["tool_result"] = "tool_result"
    id: str
    ok: bool
    output: str = ""


class SkillActivated(StrictModel):
    type: Literal["skill_activated"] = "skill_activated"
    skill: str
    evidence: Evidence = "native"


class SubagentSpawned(StrictModel):
    type: Literal["subagent_spawned"] = "subagent_spawned"
    subagent: str
    evidence: Evidence = "native"


class FileChange(StrictModel):
    type: Literal["file_change"] = "file_change"
    path: str = Field(description="absolute path inside the workspace")
    change: ChangeKind


class HookFired(StrictModel):
    type: Literal["hook_fired"] = "hook_fired"
    event: str
    output: str = ""


class Usage(StrictModel):
    type: Literal["usage"] = "usage"
    input_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0
    cost_usd: NonNegativeFloat | None = Field(
        None, description="None when the driver can't price it"
    )


class Log(StrictModel):
    type: Literal["log"] = "log"
    level: Literal["debug", "info", "warning"]
    text: str


class Error(StrictModel):
    type: Literal["error"] = "error"
    message: str


class End(StrictModel):
    type: Literal["end"] = "end"
    status: EndStatus
    reason: str = ""


class CheckResult(StrictModel):
    type: Literal["check_result"] = "check_result"
    name: str
    passed: bool
    detail: str = ""


type Payload = Annotated[
    Start
    | Message
    | ToolCall
    | ToolResult
    | SkillActivated
    | SubagentSpawned
    | FileChange
    | HookFired
    | Usage
    | Log
    | Error
    | End
    | CheckResult,
    Field(discriminator="type"),
]

PAYLOAD = TypeAdapter[Payload](Payload)


class Event(StrictModel):
    """One line of a trial's run log."""

    run: str
    trial: str
    seq: PositiveInt
    t: AwareDatetime
    payload: Payload
