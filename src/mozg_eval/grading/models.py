"""What a grader says about one trial; stored by the ResultStore, read by stats and reports."""

from typing import Literal

from pydantic import Field, NonNegativeFloat, NonNegativeInt

from mozg_eval.spec.models import Arm, StrictModel

type Verdict = Literal["pass", "fail", "n/a"]


class GradeResult(StrictModel):
    name: str
    verdict: Verdict
    detail: str = ""
    weight: float = 1.0
    arm: Arm = "both"


class JudgeSpend(StrictModel):
    """What one judge call cost; a cache hit costs nothing and has no row."""

    model: str
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    cost_usd: NonNegativeFloat | None = Field(description="None when the price is unknown")
