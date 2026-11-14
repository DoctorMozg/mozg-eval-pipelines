"""JUnit XML with one testcase per case, so CI reporters agree with mzeval's exit code."""

import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence

from mozg_eval.grading.models import GradeResult
from mozg_eval.grading.stats import SuiteStats
from mozg_eval.storage.base import TrialRecord


class JUnitReport:
    def __init__(self, suite: str, threshold: float) -> None:
        self.suite = suite
        self.threshold = threshold

    def build(
        self,
        trials: Sequence[TrialRecord],
        grades: Mapping[str, Sequence[GradeResult]],
        skipped: Mapping[str, str],
    ) -> str:
        """`trials` are the gated ones: in an ablation run, only the with-bundle arm."""
        stats = SuiteStats.of(trials, self.threshold)
        root = ET.Element("testsuites")
        suite = ET.SubElement(root, "testsuite", name=self.suite)
        counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
        for case in stats.cases:
            runs = sorted((t for t in trials if t.case == case.case), key=lambda t: t.repeat)
            testcase = ET.SubElement(suite, "testcase", classname=self.suite, name=case.case)
            counts["tests"] += 1
            lines = [self._trial_line(t, grades.get(t.id, [])) for t in runs]
            errored = [t for t in runs if t.status in ("error", "timeout")]
            if errored:
                counts["errors"] += 1
                message = f"{len(errored)} of {case.runs} runs ended in error or timeout"
                ET.SubElement(testcase, "error", message=message).text = "\n".join(lines)
            elif case.pass_at_1 < self.threshold:
                counts["failures"] += 1
                message = (
                    f"{case.passes} of {case.runs} runs passed, "
                    f"below the threshold of {self.threshold:.0%}"
                )
                ET.SubElement(testcase, "failure", message=message).text = "\n".join(lines)
            ET.SubElement(testcase, "system-out").text = "\n".join(lines)
        for name, reason in sorted(skipped.items()):
            testcase = ET.SubElement(suite, "testcase", classname=self.suite, name=name)
            ET.SubElement(testcase, "skipped", message=reason)
            counts["tests"] += 1
            counts["skipped"] += 1
        for element in (root, suite):
            element.attrib |= {key: str(value) for key, value in counts.items()}
        ET.indent(root)
        return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"

    @staticmethod
    def _trial_line(trial: TrialRecord, grades: Sequence[GradeResult]) -> str:
        verdict = "pass" if trial.passed else "fail"
        failed = "; ".join(f"{g.name}: {g.detail}" for g in grades if g.verdict == "fail")
        return f"#{trial.repeat} {verdict} ({trial.status})" + (f": {failed}" if failed else "")
