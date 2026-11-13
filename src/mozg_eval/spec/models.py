import re
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    model_validator,
)

SCHEMA = "mzeval/2"

type CaseKind = Literal["fire", "near-miss", "gate", "behaviour", "trap"]
# Canonical tool kinds; each driver maps its own tool names onto these.
type ToolKind = Literal[
    "skill", "subagent", "read", "write", "edit", "glob", "grep", "shell", "ask_user"
]
# In a with/without-bundle (ablation) run, "both" graders count in both arms' scores and "with"
# graders are reported as indicators only. Outside ablation runs every grader counts.
type Arm = Literal["both", "with"]
type Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")]
type Name = Annotated[str, Field(min_length=1)]


def _check_regex(pattern: str | None, flags: str = "") -> None:
    if pattern is None:
        return
    try:
        re.compile(pattern, regex_flags(flags))
    except re.error as exc:
        raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc


def regex_flags(flags: str) -> re.RegexFlag:
    result = re.RegexFlag(0)
    for letter in flags:
        result |= {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}[letter]
    return result


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Count(StrictModel):
    """How many matches pass: at least `min`, and at most `max` when it is set."""

    min: NonNegativeInt = 1
    max: NonNegativeInt | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.max is not None and self.max < self.min:
            raise ValueError(f"max ({self.max}) is below min ({self.min})")
        return self


class Named(StrictModel):
    name: Slug
    arm: Arm = "both"
    weight: PositiveFloat = 1.0


class GraderBase(Count, Named):
    pass


class SkillGrader(GraderBase):
    type: Literal["skill"]
    arm: Arm = "with"  # a skill can't fire without its plugin, so ablation scores leave it out
    skill: Name


class SubagentGrader(GraderBase):
    type: Literal["subagent"]
    subagent: Name


class ToolCalledGrader(GraderBase):
    type: Literal["tool_called"]
    kind: ToolKind
    match: str | None = Field(None, description="regex over the tool input as compact JSON")
    path_match: str | None = Field(None, description="regex over the file path the tool touches")

    @model_validator(mode="after")
    def _regexes(self) -> Self:
        _check_regex(self.match)
        _check_regex(self.path_match)
        return self


class ToolRef(StrictModel):
    kind: ToolKind
    match: str | None = Field(None, description="regex over the tool input as compact JSON")

    @model_validator(mode="after")
    def _regex(self) -> Self:
        _check_regex(self.match)
        return self


class ToolOrderGrader(Named):
    type: Literal["tool_order"]
    sequence: list[ToolKind | ToolRef] = Field(
        min_length=2,
        description="each step's first call comes after the previous step's first call",
    )


class FileTarget(StrictModel):
    file: Name


type RegexTarget = Literal["final", "transcript", "created_files"] | FileTarget


class RegexGrader(GraderBase):
    type: Literal["regex"]
    pattern: Name
    target: RegexTarget = "final"
    flags: Annotated[str, Field(pattern=r"^[imsx]*$")] = ""

    @model_validator(mode="after")
    def _regex(self) -> Self:
        _check_regex(self.pattern, self.flags)
        return self


class RawRegexGrader(GraderBase):
    """Regex over one driver's raw stream, for the checks no canonical event covers."""

    type: Literal["raw_regex"]
    driver: Name
    pattern: Name
    flags: Annotated[str, Field(pattern=r"^[imsx]*$")] = ""

    @model_validator(mode="after")
    def _regex(self) -> Self:
        _check_regex(self.pattern, self.flags)
        return self


class HookFiredGrader(GraderBase):
    type: Literal["hook_fired"]
    event: Name
    match: str | None = Field(None, description="regex over the hook's input and output")

    @model_validator(mode="after")
    def _regex(self) -> Self:
        _check_regex(self.match)
        return self


class FileExistsGrader(Named):
    type: Literal["file_exists"]
    path: Name = Field(description="glob over workspace files the agent created during the run")
    exists: bool = True


class CommandGrader(Named):
    type: Literal["command"]
    run: Name = Field(description="shell command run in the workspace after the session")
    timeout_s: PositiveInt = 60


class JudgeGrader(Named):
    type: Literal["judge"]
    checklist: list[Name] = Field(min_length=1)
    focus: RegexTarget = Field("transcript", description="what the judge reads")
    baseline: str | None = Field(
        None, description="reference transcript (.jsonl); the run must be at least as good"
    )
    model: str | None = None


type Grader = Annotated[
    SkillGrader
    | SubagentGrader
    | ToolCalledGrader
    | ToolOrderGrader
    | RegexGrader
    | RawRegexGrader
    | HookFiredGrader
    | FileExistsGrader
    | CommandGrader
    | JudgeGrader,
    Field(discriminator="type"),
]


class SkillNotExpected(StrictModel):
    not_: Name = Field(alias="not")
    owner: str | None = Field(None, description="the skill that must fire instead")


class Expect(StrictModel):
    """Routing label: graded, and the input to per-skill precision, recall and confusion matrix."""

    skill: Name | SkillNotExpected | None = None
    arm: Arm = "with"


type EnvName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")]


class Execution(StrictModel):
    prompt: Name
    tools: list[ToolKind] = []
    max_turns: Annotated[int, Field(ge=1, le=200)] = 10
    timeout_s: Annotated[int, Field(ge=1, le=3600)] = 300
    runs: Annotated[int, Field(ge=1, le=50)] = 3
    model: str | None = None
    append_system_prompt: str | None = None
    env: dict[EnvName, str] = {}


class DriverFilter(StrictModel):
    only: list[Name] | None = None
    exclude: list[Name] = []

    @model_validator(mode="after")
    def _one_of(self) -> Self:
        if self.only is not None and self.exclude:
            raise ValueError("set either only or exclude, not both")
        return self


class Case(StrictModel):
    schema_: Literal["mzeval/2"] = Field(alias="schema")
    name: Slug
    description: str = ""
    expected_outcome: str = ""
    kind: CaseKind
    tags: list[Name] = []
    expect: Expect = Expect()
    bundle: list[Name] = Field([], description="plugin directories loaded for the case")
    fixture: str | None = Field(None, description="script that prepares the workspace")
    history_file: str | None = Field(
        None, description="session (.jsonl) to resume before the prompt"
    )
    add_dirs: list[Name] = Field([], description="read-only directories from the case dir")
    execution: Execution
    graders: list[Grader] = []
    drivers: DriverFilter = DriverFilter()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        names = [grader.name for grader in self.graders]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"grader names must be unique: {', '.join(duplicates)}")
        skill = self.expect.skill
        if self.kind == "fire" and not isinstance(skill, str):
            raise ValueError("a fire case needs expect.skill set to the skill that should fire")
        if self.kind == "near-miss" and not isinstance(skill, SkillNotExpected):
            raise ValueError("a near-miss case needs expect.skill.not")
        if skill is None and not self.graders:
            raise ValueError("the case grades nothing: set expect.skill or add graders")
        return self
