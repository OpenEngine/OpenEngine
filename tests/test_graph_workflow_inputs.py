from engine.graph_runtime.inputs import (
    LEAST_UTILIZED, ROUND_ROBIN, WorkflowInput, choose_runners, mode_input, offer_runners,
    resolve_inputs,
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


def test_least_utilized_rotates_when_no_runner_has_a_reading():
    turns: dict[str, int] = {}
    inputs = {"implementation_runner": LEAST_UTILIZED, "review_runner": "codex"}
    picked = [
        choose_runners(DECLARED, inputs, usage=dict, turns=turns)["implementation_runner"]
        for _ in range(3)
    ]
    assert picked == ["codex", "claude", "codex"]


def test_round_robin_rotates_each_input_through_its_runners():
    turns: dict[str, int] = {}
    inputs = {"implementation_runner": ROUND_ROBIN, "review_runner": "claude"}
    picked = [
        choose_runners(DECLARED, inputs, usage=dict, turns=turns)["implementation_runner"]
        for _ in range(3)
    ]
    assert picked == ["codex", "claude", "codex"]
    assert turns == {"implementation_runner": 3}


def test_added_runners_are_offered_before_the_policies_of_accepting_inputs_only():
    mode = mode_input()
    accepting = ("implementation_runner", "review_runner")
    offered = offer_runners((*DECLARED, mode), ("qwen", "codex"), accepting)
    assert offered[0].choices == ("codex", "claude", "qwen", LEAST_UTILIZED, ROUND_ROBIN)
    assert offered[1].choices == offered[0].choices
    assert offered[2] == mode
    assert offer_runners(DECLARED, (), accepting) == DECLARED
    assert offer_runners(DECLARED, ("qwen",), ()) == DECLARED


def test_added_runners_leave_a_free_text_runner_input_accepting_any_runner():
    free = WorkflowInput("implementation_runner", "Implementation runner", "codex")
    offered = offer_runners((free,), ("qwen",), (free.name,))
    assert offered == (free,)
    assert resolve_inputs(offered, {}) == {"implementation_runner": "codex"}
    assert resolve_inputs(offered, {"implementation_runner": "claude"}) == {
        "implementation_runner": "claude"
    }


def test_added_runners_join_the_available_policy_pool():
    turns: dict[str, int] = {}
    inputs = {"implementation_runner": LEAST_UTILIZED, "review_runner": ROUND_ROBIN}
    picked = [
        tuple(choose_runners(
            offer_runners(DECLARED, ("qwen",), inputs), inputs,
            usage=dict, turns=turns, available=("claude", "qwen"),
        ).values())
        for _ in range(3)
    ]
    assert picked == [("claude", "claude"), ("qwen", "qwen"), ("claude", "claude")]



def test_only_available_runner_is_used_for_both_stages():
    inputs = {item.name: ROUND_ROBIN for item in DECLARED}
    chosen = choose_runners(DECLARED, inputs, usage=dict, turns={}, available=("claude",))
    assert set(chosen.values()) == {"claude"}


def test_no_available_runner_refuses_with_signin_hint():
    import pytest

    for policy in (ROUND_ROBIN, LEAST_UTILIZED):
        with pytest.raises(ValueError, match="oe agent signin"):
            choose_runners(DECLARED, {"implementation_runner": policy},
                           usage=dict, turns={}, available=())


def test_unavailable_runner_cannot_win_by_utilization():
    chosen = choose_runners(DECLARED, {"implementation_runner": LEAST_UTILIZED},
                            usage=lambda: {"codex": 0, "claude": 90}, turns={},
                            available=("claude",))
    assert chosen["implementation_runner"] == "claude"
