"""Which assertions a driver's events can answer; the rest grade n/a, never fail."""

from mozg_eval.drivers.protocol import Capabilities
from mozg_eval.spec.harness import Harness
from mozg_eval.spec.models import (
    Case,
    Grader,
    HookFiredGrader,
    RawRegexGrader,
    SkillGrader,
    SubagentGrader,
    ToolCalledGrader,
    ToolKind,
    ToolOrderGrader,
    ToolRef,
)


class DriverSupport:
    def __init__(self, driver: str, capabilities: Capabilities) -> None:
        self.driver = driver
        self.capabilities = capabilities

    def harness_gap(self, harness: Harness) -> str | None:
        """Why this driver can't load the harness, or None when it loads every part."""
        if missing := sorted(harness.parts() - set(self.capabilities.harness)):
            return f"{self.driver} can't load harness {harness.name}: {', '.join(missing)}"
        return None

    def expect_gap(self, case: Case) -> str | None:
        if case.expect.skill is not None and self.capabilities.skills == "none":
            return f"{self.driver} reports no skill activations"
        return None

    def gap(self, grader: Grader) -> str | None:
        """Why this driver can't answer the grader, or None when it can."""
        caps = self.capabilities
        match grader:
            case SkillGrader() if caps.skills == "none":
                return f"{self.driver} reports no skill activations"
            case SubagentGrader() if caps.subagents == "none":
                return f"{self.driver} reports no subagent names"
            case HookFiredGrader() if caps.hooks == "none":
                return f"{self.driver} reports no hooks"
            case RawRegexGrader() if grader.driver != self.driver:
                return f"raw stream of driver {grader.driver} not available"
            case ToolCalledGrader():
                return self._tool_gap(grader.kind, grader.match)
            case ToolOrderGrader():
                for step in grader.sequence:
                    ref = step if isinstance(step, ToolRef) else ToolRef(kind=step, match=None)
                    if gap := self._tool_gap(ref.kind, ref.match):
                        return gap
        return None

    def skip_reason(self, case: Case) -> str | None:
        """Why the case can't run on this driver yet, or None."""
        drivers = case.drivers
        if (drivers.only is not None and self.driver not in drivers.only) or (
            self.driver in drivers.exclude
        ):
            return f"case excludes driver {self.driver}"
        if case.history_file or case.add_dirs:
            return "history_file and add_dirs are not supported yet"
        tools = case.execution.tools
        if not self.capabilities.host_shell and (tools is None or "shell" in tools):
            # No tool list means the agent's defaults, which include its shell.
            return f"{self.driver} has no sandbox; cases with shell wait for Docker isolation"
        if not self.applicable(case):
            # Every grade would be n/a, so the trial could only pass vacuously.
            return f"nothing to grade on {self.driver}: {'; '.join(self.gaps(case))}"
        return None

    def applicable(self, case: Case) -> bool:
        """Whether the driver can answer at least one of the case's assertions."""
        has_expect = case.expect.skill is not None and self.expect_gap(case) is None
        return has_expect or any(self.gap(g) is None for g in case.graders)

    def gaps(self, case: Case) -> list[str]:
        found = [self.expect_gap(case), *(self.gap(g) for g in case.graders)]
        return sorted({gap for gap in found if gap})

    def _tool_gap(self, kind: ToolKind, match: str | None) -> str | None:
        if kind not in self.capabilities.tools:
            return f"{self.driver} reports no {kind} calls"
        if match is not None and kind not in self.capabilities.tool_input:
            return f"{self.driver} {kind} input has its own shape; `match` can't apply"
        return None
