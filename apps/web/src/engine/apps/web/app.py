"""The Streamlit script. `engine-web` runs this file.

Unwired on purpose. Every page draws from `readmodel.ReadModel`, and the one
`composition.build_read_model` hands back today is empty; nothing here opens a
connection, starts a run, or calls a capability. Where a control would normally
act, it shows what it *would* send and stops -- which is more useful than a
disabled button, because it makes the vocabulary the interface will speak
reviewable now.

Two things on screen are real, and neither reaches the outside world: the Wiring
page reads the *types* in the composed capability graph, and the request form
runs `engine.core.decide`, which is a pure function from (state, event) to
commands. Both are computation over the code that is already here.

Streamlit reruns this module top to bottom on every interaction, so it stays a
script -- no caching, no session state, nothing that has to survive a rerun.
"""

from dataclasses import fields

import streamlit as st

from engine.apps.web.composition import Settings, build_capabilities, build_read_model
from engine.apps.web.readmodel import DemoReadModel, ReadModel, RunSummary
from engine.core import decide
from engine.domain import RunId, RunPhase, RunRequested, RunState, TaskId

#: Sidebar order, and the dispatch table at the bottom of the file.
PAGES = ("Runs", "Request a run", "Inbox", "Wiring")

#: Ids used to build the previewed request. A real one is minted by whatever
#: accepts the run; nothing here is allowed to.
PREVIEW_RUN_ID = RunId("run-preview")
PREVIEW_TASK_ID = TaskId("task-preview")

st.set_page_config(page_title="engine", page_icon="⚙", layout="wide")


# --- pages -----------------------------------------------------------------


def runs_page(model: ReadModel) -> None:
    st.title("Runs")
    runs = model.runs()

    counts = st.columns(4)
    counts[0].metric("Runs", len(runs))
    counts[1].metric("In flight", sum(1 for run in runs if not run.is_terminal))
    counts[2].metric("Succeeded", sum(1 for run in runs if run.phase is RunPhase.SUCCEEDED))
    counts[3].metric("Failed", sum(1 for run in runs if run.phase is RunPhase.FAILED))

    if not runs:
        st.info(
            "No runs to show. This interface is not reading from anything yet -- "
            "turn on **Demo data** in the sidebar to see the layout with rows in it."
        )
        return

    st.dataframe(
        [
            {
                "run": run.run_id,
                "task": run.task_id,
                "phase": run.phase.value,
                "repository": run.repository,
                "attempts": run.attempts,
                "prompt": run.prompt,
            }
            for run in runs
        ],
        hide_index=True,
    )

    st.divider()
    selected = st.selectbox("Run", [run.run_id for run in runs])
    run = next(run for run in runs if run.run_id == selected)
    _run_detail(run)


def _run_detail(run: RunSummary) -> None:
    st.subheader(run.run_id)
    detail = st.columns(4)
    detail[0].metric("Phase", run.phase.value)
    detail[1].metric("Task", run.task_id)
    detail[2].metric("Attempts", run.attempts)
    detail[3].metric("Repository", run.repository)

    st.caption("Prompt")
    st.write(run.prompt)

    if run.review_url:
        st.caption("Review")
        st.write(run.review_url)


def request_page(model: ReadModel) -> None:
    st.title("Request a run")
    st.caption(
        "The form does not start anything. It builds the domain event a request "
        "becomes and shows what the engine decides to do with it."
    )

    with st.form("request-run"):
        repository = st.text_input("Repository", placeholder="acme/api")
        prompt = st.text_area("What should the agent do?", placeholder="fix the flaky auth test")
        submitted = st.form_submit_button("Preview request")

    if not submitted:
        return
    if not repository or not prompt:
        st.error("Repository and prompt are both required.")
        return

    event = RunRequested(
        run_id=PREVIEW_RUN_ID,
        task_id=PREVIEW_TASK_ID,
        prompt=prompt,
        repository=repository,
    )
    _, commands = decide(RunState(run_id=PREVIEW_RUN_ID, task_id=PREVIEW_TASK_ID), event)

    st.warning(
        "Nothing was sent. Ingress lands with the control-server ticket; below is "
        "the event this form will emit and the commands `engine.core.decide` "
        "returns for it, computed in process and dispatched nowhere."
    )
    st.caption("Event")
    st.code(repr(event), language="python")
    st.caption("Commands")
    st.code("\n".join(repr(command) for command in commands) or "(none)", language="python")


def inbox_page(model: ReadModel) -> None:
    st.title("Inbox")
    st.caption(
        "Questions an agent stopped to ask. An answer here does not wake the "
        "agent directly -- it becomes an event that re-enters the workflow, which "
        "is what lets the same agent instance resume in the same workspace."
    )

    clarifications = model.clarifications()
    if not clarifications:
        st.info(
            "Nothing waiting. The interface is the intended first implementation "
            "of the Communications capability; delivery lands with that ticket."
        )
        return

    for index, clarification in enumerate(clarifications):
        with st.container(border=True):
            st.markdown(f"**{clarification.asked_by}** on run `{clarification.run_id}`")
            st.write(clarification.question)
            st.text_area("Answer", key=f"answer-{index}", label_visibility="collapsed")
            if st.button("Send answer", key=f"send-{index}"):
                st.warning("Not sent: replies land with the communications ticket.")


def wiring_page(model: ReadModel) -> None:
    st.title("Wiring")
    st.caption(
        "What this process composed at startup: one implementation per port, "
        "chosen in `composition.py` and nowhere else. Reading it is introspection "
        "-- no capability is called."
    )

    capabilities = build_capabilities(SETTINGS)
    st.dataframe(
        [
            {
                "capability": field.name,
                "port": getattr(field.type, "__name__", str(field.type)),
                "implementation": type(getattr(capabilities, field.name)).__name__,
                "module": type(getattr(capabilities, field.name)).__module__,
            }
            for field in fields(capabilities)
        ],
        hide_index=True,
    )
    st.caption(
        "Constructed, not connected: every adapter above is a placeholder whose "
        "methods raise `NotImplementedError`."
    )


# --- shell -----------------------------------------------------------------

SETTINGS = Settings()

PAGE_BODIES = {
    "Runs": runs_page,
    "Request a run": request_page,
    "Inbox": inbox_page,
    "Wiring": wiring_page,
}


def main() -> None:
    st.sidebar.title("engine")
    st.sidebar.caption("control interface")
    page = st.sidebar.radio("Page", PAGES, label_visibility="collapsed")

    demo = st.sidebar.toggle(
        "Demo data",
        value=False,
        help="Fill the pages with fixed, invented rows so the layout can be judged.",
    )
    st.sidebar.divider()
    if demo:
        st.sidebar.warning("Showing demo data. None of it came from a running engine.")
    else:
        st.sidebar.info("Not wired: no live data source is connected yet.")

    model: ReadModel = DemoReadModel() if demo else build_read_model(SETTINGS)
    PAGE_BODIES[page](model)


main()
