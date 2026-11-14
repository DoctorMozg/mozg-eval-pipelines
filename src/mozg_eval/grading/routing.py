"""Skill routing across a run's trials: per-skill precision, recall, F1 and a confusion matrix."""

from collections.abc import Sequence
from typing import Self

from mozg_eval.events.models import bare_name
from mozg_eval.spec.models import Case, SkillNotExpected, StrictModel

NONE = "(none)"


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


class RoutedTrial(StrictModel):
    expected: str
    decoy: str | None = None
    fired: frozenset[str]

    @classmethod
    def of(cls, case: Case, fired: Sequence[str]) -> Self | None:
        """None for a case that carries no routing label."""
        skill = case.expect.skill
        if skill is None:
            return None
        if isinstance(skill, SkillNotExpected):
            owner = NONE if skill.owner is None else bare_name(skill.owner)
            return cls(expected=owner, decoy=bare_name(skill.not_), fired=frozenset(fired))
        return cls(expected=bare_name(skill), fired=frozenset(fired))


class SkillRouting(StrictModel):
    skill: str
    hits: int
    misses: int
    false_triggers: int
    near_miss_triggers: int
    near_miss_trials: int

    @property
    def precision(self) -> float | None:
        return _ratio(self.hits, self.hits + self.false_triggers)

    @property
    def recall(self) -> float | None:
        return _ratio(self.hits, self.hits + self.misses)

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or p + r == 0:
            return None
        return 2 * p * r / (p + r)


class Routing(StrictModel):
    skills: list[SkillRouting]
    confusion: dict[str, dict[str, int]]

    @classmethod
    def of(cls, trials: Sequence[RoutedTrial]) -> Self:
        names = {t.expected for t in trials} | {t.decoy for t in trials if t.decoy}
        names |= {skill for t in trials for skill in t.fired}
        skills = [cls._skill(name, trials) for name in sorted(names - {NONE})]
        confusion: dict[str, dict[str, int]] = {}
        for t in trials:
            row = confusion.setdefault(t.expected, {})
            for fired in t.fired or {NONE}:
                row[fired] = row.get(fired, 0) + 1
        return cls(skills=skills, confusion=confusion)

    @staticmethod
    def _skill(name: str, trials: Sequence[RoutedTrial]) -> SkillRouting:
        mine = [t for t in trials if t.expected == name]
        decoyed = [t for t in trials if t.decoy == name]
        return SkillRouting(
            skill=name,
            hits=sum(name in t.fired for t in mine),
            misses=sum(name not in t.fired for t in mine),
            false_triggers=sum(t.expected != name and name in t.fired for t in trials),
            near_miss_triggers=sum(name in t.fired for t in decoyed),
            near_miss_trials=len(decoyed),
        )

    def summary(self) -> list[str]:
        lines = []
        for s in self.skills:
            easy = s.false_triggers - s.near_miss_triggers
            lines.append(
                f"{s.skill}: precision {_fmt(s.precision)}, recall {_fmt(s.recall)}, "
                f"F1 {_fmt(s.f1)}; near-miss triggers {s.near_miss_triggers}/{s.near_miss_trials}"
                f", other false triggers {easy}"
            )
        columns = sorted({fired for row in self.confusion.values() for fired in row})
        header = ["expected \\ fired", *columns]
        rows = [
            [expected, *(str(self.confusion[expected].get(c, 0)) for c in columns)]
            for expected in sorted(self.confusion)
        ]
        widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
        for row in [header, *rows]:
            cells = (cell.ljust(w) for cell, w in zip(row, widths, strict=True))
            lines.append("  ".join(cells).rstrip())
        return lines
