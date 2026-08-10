"""The Streamlit script runs.

Streamlit scripts fail at render time, not import time, so nothing else in this
suite would notice `app.py` raising -- a bad `st.` call, a page function with the
wrong signature, a composition that blows up on construction. `AppTest` executes
the script the way the server does and re-raises what it caught.

No chat message is sent here: submitting one would shell out to the real Codex
CLI. What the turn itself does is covered in `test_agent_chat.py` against a
scripted runner.
"""

import pytest

pytest.importorskip("streamlit", reason="the web app is an optional workspace member")

from pathlib import Path  # noqa: E402

from streamlit.testing.v1 import AppTest  # noqa: E402

APP = Path(__file__).resolve().parent.parent / "apps/web/src/engine/apps/web/app.py"

#: Generous: the script composes adapters and starts a conversation, and CI
#: machines are slow. It never calls a model.
TIMEOUT_SECONDS = 60


def _run(page: str | None = None) -> AppTest:
    app = AppTest.from_file(str(APP), default_timeout=TIMEOUT_SECONDS).run()
    if page is not None:
        app.sidebar.radio[0].set_value(page).run()
    return app


def test_the_script_renders() -> None:
    app = _run()
    assert not app.exception


def test_chat_is_the_landing_page_and_starts_a_conversation() -> None:
    app = _run()

    assert app.title[0].value == "Chat"
    assert app.chat_input, "there is nowhere to type"
    assert app.session_state["instances"], "no agent instance was started"


def test_every_agent_can_be_chosen() -> None:
    """`options` comes back formatted, so compare the ids the labels start with."""
    app = _run()
    labels = list(app.selectbox[0].options)
    assert {label.split(" — ")[0] for label in labels} == {"coder", "foreman"}

    app.selectbox[0].set_value("foreman").run()

    assert not app.exception
    assert "foreman" in app.session_state["instances"]


@pytest.mark.parametrize("page", ["Runs", "Request a run", "Inbox", "Wiring"])
def test_the_other_pages_render(page: str) -> None:
    app = _run(page)

    assert not app.exception
    assert app.title[0].value == page


def test_the_wiring_page_reports_what_was_composed() -> None:
    app = _run("Wiring")

    wired = app.dataframe[0].value
    assert dict(zip(wired["capability"], wired["implementation"])) == {
        "workflow_runtime": "TemporalWorkflowRuntime",
        "source_control": "GitHubSourceControl",
        "agent_runner": "CodexAgentRunner",
        "communications": "BuzzCommunications",
        "workspace_provider": "GitWorktreeWorkspaceProvider",
        "state_store": "InMemoryStateStore",
    }


def test_demo_data_fills_the_run_pages() -> None:
    """The toggle exists so the layout is reviewable before runs are recorded."""
    app = _run("Runs")
    assert app.info, "an unwired page should say so"

    app.sidebar.toggle[0].set_value(True).run()

    assert not app.exception
    assert app.warning, "demo rows must be labelled as invented"
    assert len(app.dataframe[0].value) == 4
