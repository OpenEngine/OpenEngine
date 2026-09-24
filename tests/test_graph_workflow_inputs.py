from engine.graph_runtime.inputs import (
    LEAST_UTILIZED, ROUND_ROBIN, WorkflowInput, choose_runners,
)

RUNNERS = ("codex", "claude", LEAST_UTILIZED, ROUND_ROBIN)
DECLARED = (
    WorkflowInput("implementation_runner", "Implementation runner", "codex", True, RUNNERS),
    WorkflowInput("review_runner", "Review runner", "claude", True, RUNNERS),
)


def test_explicit_runners_pass_through_without_reading_usage():
    def unread():
        raise AssertionError("usage read for explicit runners")

    inputs = {"implementation_runner": "claude", "review_runner": "codex"}
    assert choose_runners(DECLARED, inputs, usage=unread, turns={}) == inputs


def test_least_utilized_picks_the_runner_with_most_headroom():
    inputs = {"implementation_runner": LEAST_UTILIZED, "review_runner": "codex"}
    chosen = choose_runners(
        DECLARED, inputs, usage=lambda: {"codex": 80.0, "claude": 20.0}, turns={},
    )
    assert chosen == {"implementation_runner": "claude", "review_runner": "codex"}


def test_least_utilized_prefers_a_read_runner_over_an_unread_one():
    chosen = choose_runners(
        DECLARED, {"implementation_runner": LEAST_UTILIZED, "review_runner": "codex"},
        usage=lambda: {"claude": 90.0}, turns={},
    )
    assert chosen["implementation_runner"] == "claude"


def test_round_robin_rotates_each_input_through_its_runners():
    turns: dict[str, int] = {}
    inputs = {"implementation_runner": ROUND_ROBIN, "review_runner": "claude"}
    picked = [
        choose_runners(DECLARED, inputs, usage=dict, turns=turns)["implementation_runner"]
        for _ in range(3)
    ]
    assert picked == ["codex", "claude", "codex"]
    assert turns == {"implementation_runner": 3}
