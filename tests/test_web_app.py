"""The web interface renders, and stays unwired.

Two claims, both worth a test:

* Every page draws without raising. A Streamlit page fails at render time, inside
  a rerun, so a broken one is invisible to every other test here -- importing the
  module proves nothing, because the script *is* the render.
* The interface reads nothing. `build_read_model` quietly starting to return data
  would change what this app is; this is the test that notices.

`AppTest` drives the script the way the server does, with no browser and no port.
"""

import pytest
from streamlit.testing.v1 import AppTest

from engine.apps.web.__main__ import APP_SCRIPT
from engine.apps.web.app import PAGES
from engine.apps.web.composition import Settings, build_read_model

#: Generous, because the first run pays for importing Streamlit itself.
TIMEOUT = 30


def render(page: str, *, demo: bool = False) -> AppTest:
    """Run the script, then navigate -- each `run()` is a rerun, as in a browser."""
    app = AppTest.from_file(str(APP_SCRIPT), default_timeout=TIMEOUT).run()
    app.sidebar.radio[0].set_value(page).run()
    if demo:
        app.sidebar.toggle[0].set_value(True).run()
    return app


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(page: str) -> None:
    app = render(page)
    assert not app.exception, [e.value for e in app.exception]


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_with_demo_data(page: str) -> None:
    """The demo rows exist to be looked at; they must survive every page."""
    app = render(page, demo=True)
    assert not app.exception, [e.value for e in app.exception]


def test_interface_is_unwired() -> None:
    """The default read model is empty. Wiring it up is a deliberate act."""
    model = build_read_model(Settings())
    assert model.runs() == ()
    assert model.clarifications() == ()


def test_runs_table_is_empty_until_demo_data_is_asked_for() -> None:
    """Nothing invented is shown unless the sidebar toggle asks for it."""
    assert not render("Runs").dataframe
    assert render("Runs", demo=True).dataframe


def test_requesting_a_run_previews_instead_of_dispatching() -> None:
    """The form's whole output is a pure `decide` call: an event, and the
    commands it produces. Nothing leaves the process."""
    app = render("Request a run")
    app.text_input[0].set_value("acme/api")
    app.text_area[0].set_value("fix the flaky auth test")
    app.button[0].click().run()

    assert not app.exception, [e.value for e in app.exception]
    previewed = "\n".join(block.value for block in app.code)
    assert "RunRequested" in previewed
    assert "ProvisionWorkspace" in previewed
    assert app.warning, "the form must say that nothing was sent"


def test_requesting_a_run_needs_a_repository_and_a_prompt() -> None:
    app = render("Request a run")
    app.button[0].click().run()

    assert not app.exception, [e.value for e in app.exception]
    assert app.error
    assert not app.code
