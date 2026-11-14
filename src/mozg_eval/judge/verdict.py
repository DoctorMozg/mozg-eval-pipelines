"""One verdict per criterion; a quote that isn't in what the judge read is sent back."""

import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import Agent, ModelRetry, RunContext, UnexpectedModelBehavior
from pydantic_ai.models import Model

from mozg_eval.judge.cache import JudgeCache

log = logging.getLogger(__name__)

# Shorter quotes, like a lone "a", occur in almost any text and so prove nothing.
MIN_QUOTE_CHARS = 8

INSTRUCTIONS = """\
You grade one run of a coding agent against a single criterion. The evidence holds what the agent
said and, depending on the criterion, what its tools returned.

- Write your reasoning first, then the verdict.
- The verdict is pass or fail when the evidence settles the criterion, and unknown when it doesn't.
- evidence_quote copies, word for word, the shortest passage of the evidence the verdict rests on.
  Never paraphrase it. Leave it empty only for unknown.
"""


class Verdict(BaseModel):
    reasoning: str  # first, so the model argues before deciding
    verdict: Literal["pass", "fail", "unknown"]
    evidence_quote: str


@dataclass
class JudgeContext:
    evidence: str


def _squashed(text: str) -> str:
    return " ".join(text.split())


def _evidence_is_real(ctx: RunContext[JudgeContext], verdict: Verdict) -> Verdict:
    if verdict.verdict == "unknown":
        return verdict
    quote = _squashed(verdict.evidence_quote)
    if len(quote) < MIN_QUOTE_CHARS:
        raise ModelRetry(
            f"A pass or fail verdict needs an evidence_quote of at least {MIN_QUOTE_CHARS} "
            "characters, copied from the evidence."
        )
    if quote not in _squashed(ctx.deps.evidence):
        raise ModelRetry(f"Quote not found in the evidence: {verdict.evidence_quote[:80]!r}")
    return verdict


class CriterionJudge:
    """Judges criteria with one model; accepted verdicts are cached, rejected ones end unknown."""

    def __init__(self, model: Model | str, cache: JudgeCache) -> None:
        self.model = model if isinstance(model, str) else f"{model.system}:{model.model_name}"
        self.cache = cache
        self._agent = Agent(
            model,
            output_type=Verdict,
            deps_type=JudgeContext,
            instructions=INSTRUCTIONS,
            retries={"output": 2},
        )
        self._agent.output_validator(_evidence_is_real)

    async def judge(self, criterion: str, evidence: str) -> Verdict:
        key = JudgeCache.key(evidence, criterion, self.model, INSTRUCTIONS)
        if cached := self.cache.get(key):
            log.debug("judge cache hit for %r", criterion[:60])
            return Verdict.model_validate_json(cached)
        prompt = f"<criterion>\n{criterion}\n</criterion>\n<evidence>\n{evidence}\n</evidence>"
        try:
            result = await self._agent.run(prompt, deps=JudgeContext(evidence))
        except UnexpectedModelBehavior as exc:
            # Not cached: the next rejudge should give the model another chance.
            log.warning(
                "judge %s gave no acceptable verdict on %r: %s", self.model, criterion[:60], exc
            )
            return Verdict(reasoning=f"rejected: {exc}", verdict="unknown", evidence_quote="")
        self.cache.put(key, result.output.model_dump_json())
        return result.output
