import pytest

from mozg_eval.grading.routing import NONE, RoutedTrial, Routing
from mozg_eval.spec.models import Case


def case(kind: str, expect: object) -> Case:
    return Case.model_validate(
        {
            "schema": "mzeval/2",
            "name": "c",
            "kind": kind,
            "expect": {"skill": expect},
            "execution": {"prompt": "p"},
            "graders": [{"name": "g", "type": "regex", "pattern": "x"}],
        }
    )


def routed(expected: str, fired: set[str], decoy: str | None = None) -> RoutedTrial:
    return RoutedTrial(expected=expected, decoy=decoy, fired=frozenset(fired))


class TestRoutedTrial:
    def test_fire_case_expects_the_bare_skill(self) -> None:
        trial = RoutedTrial.of(case("fire", "mz-memory:memory-note"), ["memory-note"])

        assert trial == routed("memory-note", {"memory-note"})

    def test_near_miss_expects_its_owner_and_names_the_decoy(self) -> None:
        owned = RoutedTrial.of(case("near-miss", {"not": "a", "owner": "p:b"}), [])
        unowned = RoutedTrial.of(case("near-miss", {"not": "a"}), [])

        assert owned == routed("b", set(), decoy="a")
        assert unowned == routed(NONE, set(), decoy="a")

    def test_unlabelled_case_is_not_routed(self) -> None:
        assert RoutedTrial.of(case("behaviour", None), ["a"]) is None


class TestRouting:
    @pytest.fixture
    def routing(self) -> Routing:
        return Routing.of(
            [
                routed("a", {"a"}),
                routed("a", set()),
                routed(NONE, {"a"}, decoy="a"),
                routed(NONE, set(), decoy="a"),
                routed("b", {"a", "b"}),
            ]
        )

    def test_per_skill_counts_and_scores(self, routing: Routing) -> None:
        a, b = routing.skills

        assert (a.hits, a.misses, a.false_triggers) == (1, 1, 2)
        assert (a.near_miss_triggers, a.near_miss_trials) == (1, 2)
        assert (a.precision, a.recall, a.f1) == pytest.approx((1 / 3, 0.5, 0.4))
        assert (b.precision, b.recall, b.f1) == (1.0, 1.0, 1.0)

    def test_confusion_counts_every_fired_skill_and_silence(self, routing: Routing) -> None:
        assert routing.confusion == {
            "a": {"a": 1, NONE: 1},
            NONE: {"a": 1, NONE: 1},
            "b": {"a": 1, "b": 1},
        }

    def test_summary_separates_near_miss_from_other_false_triggers(self, routing: Routing) -> None:
        lines = routing.summary()

        assert lines[0] == (
            "a: precision 0.33, recall 0.50, F1 0.40; "
            "near-miss triggers 1/2, other false triggers 1"
        )
        assert lines[2].split() == ["expected", "\\", "fired", "(none)", "a", "b"]
        assert lines[3].split() == ["(none)", "1", "1", "0"]

    def test_skill_never_expected_has_no_recall(self) -> None:
        (a,) = Routing.of([routed(NONE, {"a"}, decoy="a")]).skills

        assert (a.precision, a.recall, a.f1) == (0.0, None, None)
