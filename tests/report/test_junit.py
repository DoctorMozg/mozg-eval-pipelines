import xml.etree.ElementTree as ET

import pytest

from mozg_eval.events.models import EndStatus
from mozg_eval.grading.models import GradeResult
from mozg_eval.report.junit import JUnitReport
from mozg_eval.storage.base import TrialRecord


def trial(case: str, repeat: int, passed: bool, status: EndStatus = "ok") -> TrialRecord:
    return TrialRecord(
        id=f"{case}-{repeat}",
        run="r",
        case=case,
        arm="with",
        repeat=repeat,
        driver="claude",
        status=status,
        passed=passed,
    )


@pytest.fixture
def suite() -> ET.Element:
    trials = [
        trial("steady", 1, True),
        trial("steady", 2, True),
        trial("flaky", 1, True),
        trial("flaky", 2, False),
        trial("crashed", 1, False, status="timeout"),
    ]
    grades = {"flaky-2": [GradeResult(name="reply", verdict="fail", detail="0 matches")]}
    xml = JUnitReport("mzeval.claude", 1.0).build(trials, grades, {"shell-case": "no sandbox"})
    root = ET.fromstring(xml)
    assert root.tag == "testsuites"
    (testsuite,) = root
    return testsuite


class TestJUnitReport:
    def test_counts_one_testcase_per_case(self, suite: ET.Element) -> None:
        assert {k: suite.get(k) for k in ("tests", "failures", "errors", "skipped")} == {
            "tests": "4",
            "failures": "1",
            "errors": "1",
            "skipped": "1",
        }

    def test_case_below_threshold_fails_with_each_run_listed(self, suite: ET.Element) -> None:
        flaky = suite.find("testcase[@name='flaky']")
        assert flaky is not None
        failure = flaky.find("failure")

        assert failure is not None
        assert failure.get("message") == "1 of 2 runs passed, below the threshold of 100%"
        assert failure.text == "#1 pass (ok)\n#2 fail (ok): reply: 0 matches"

    def test_timeout_is_an_error_and_skip_keeps_its_reason(self, suite: ET.Element) -> None:
        assert suite.find("testcase[@name='crashed']/error") is not None
        assert suite.find("testcase[@name='steady']/failure") is None
        skipped = suite.find("testcase[@name='shell-case']/skipped")
        assert skipped is not None
        assert skipped.get("message") == "no sandbox"

    def test_lower_threshold_lets_a_flaky_case_pass(self) -> None:
        trials = [trial("flaky", 1, True), trial("flaky", 2, False)]
        root = ET.fromstring(JUnitReport("s", 0.5).build(trials, {}, {}))

        assert root.find("testsuite/testcase/failure") is None
