"""Backoff spacing for durable delivery workers."""

from openengine.messaging.retry import Backoff


def test_backoff_doubles_to_its_cap_and_resets_on_success() -> None:
    backoff = Backoff(initial=0.25, maximum=10.0)

    assert [backoff.delay(n) for n in (1, 2, 3, 7, 50)] == [0.25, 0.5, 1.0, 10.0, 10.0]
    assert backoff.due("a")
    backoff.failed("a")
    assert not backoff.due("a") and backoff.due("b")
    backoff.done("a")
    assert backoff.due("a")
