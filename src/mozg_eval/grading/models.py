"""What a grader says about one trial; stored by the ResultStore, read by stats and reports."""

from typing import Literal

from mozg_eval.spec.models import Arm, StrictModel

type Verdict = Literal["pass", "fail", "n/a"]


class GradeResult(StrictModel):
    name: str
    verdict: Verdict
    detail: str = ""
    weight: float = 1.0
    arm: Arm = "both"
