"""The Streamlit script. `engine-web` runs this file.

One page is wired and the rest are not, which the interface says out loud rather
than leaving you to find out by clicking.

**Chat** is real: it talks to an agent through `engine.runtime.AgentSession`,
which loads the conversation from the state store, runs one turn on whichever
`AgentRunner` this process composed, and stores the reply. The page itself knows
none of that -- it knows a session, a profile, and a list of messages.

**Runs**, **Inbox** and **Request a run** are not. They draw from
`readmodel.ReadModel`, and the one `composition.build_read_model` hands back is
empty. Where a control would normally act, they show what would be sent and
stop, which is more useful than a disabled button because it makes the
vocabulary reviewable now.

Streamlit reruns this module top to bottom on every interaction, so anything
that has to survive a rerun is either in `st.session_state` (which conversation
you are in) or behind `@st.cache_resource` (the composed capabilities, and with
them the in-memory store holding the conversation itself).
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import fields

import streamlit as st

from engine.apps.web.composition import (
    Settings,
    build_capabilities,
    build_read_model,
    build_runners,
    build_session,
)
from engine.apps.web.readmodel import DemoReadModel, ReadModel, RunSummary
from engine.core import decide
from engine.domain import (
    AgentId,
    AgentInstanceId,
    Message,
    Role,
    RunId,
    RunPhase,
    RunRequested,
    RunState,
    TaskId,
)
from engine.ports import AgentRunner, AgentTurn
from engine.runtime import AgentSession, Capabilities

#: Sidebar order. Chat first: it is the one that does something.
PAGES = ("Chat", "Runs", "Request a run", "Inbox", "Wiring")

#: Ids used to build the previewed request. A real one is minted by whatever
#: accepts the run; nothing here is allowed to.
PREVIEW_RUN_ID = RunId("run-preview")
PREVIEW_TASK_ID = TaskId("task-preview")

#: Which `st.chat_message` avatar each role is drawn as.
CHAT_ROLES = {Role.USER: "user", Role.ASSISTANT: "assistant"}

SETTINGS = Settings()

st.set_page_config(page_title="engine", page_icon="⚙", layout="wide")


@st.cache_resource
def wiring(_settings: Settings) -> tuple[Capabilities, Mapping[str, AgentRunner], AgentSession]:
    """Compose once per process, not once per rerun.

    The cache is what makes the conversation persist: the in-memory state store
    lives inside these capabilities, so rebuilding them on every keystroke would
    hand each rerun an empty history. The argument is underscore-prefixed
    because Streamlit hashes cache arguments and there is only ever one settings
    object here.
    """
    capabilities = build_capabilities(_settings)
    runners = build_runners(_settings)
    return capabilities, runners, build_session(capabilities, runners)


# --- chat -------------------------------------------------------------------


def _instance_for(session: AgentSession, agent_id: AgentId) -> AgentInstanceId:
    """The conversation this browser session is having with that agent.

    One instance per agent, remembered across reruns. Starting a second
    conversation with the same agent is what the New conversation button does,
    and the old instance stays in the store rather than being deleted.
    """
    instances: dict[AgentId, AgentInstanceId] = st.session_state.setdefault("instances", {})
    if agent_id not in instances:
        instances[agent_id] = asyncio.run(session.start(agent_id)).instance_id
    return instances[agent_id]


def chat_page(session: AgentSession, runners: Mapping[str, AgentRunner]) -> None:
    st.title("Chat")

    agent_ids = sorted(session.profiles)
    header = st.columns([3, 1, 1])
    agent_id = header[0].selectbox(
        "Agent",
        agent_ids,
        format_func=lambda a: f"{a} — {session.profiles[a].description}",
    )
    runner = header[1].selectbox(
        "Runner",
        session.runners,
        help=(
            "Which agent runner answers. Switching mid-conversation is fine: the "
            "transcript is ours, so the next one is handed everything the last "
            "one said and did."
        ),
    )
    profile = session.profiles[agent_id]
    instance_id = _instance_for(session, agent_id)

    header[2].write("")  # push the button down to sit level with the selectboxes
    if header[2].button("New conversation", use_container_width=True):
        st.session_state["instances"].pop(agent_id, None)
        st.rerun()

    st.caption(
        f"Instance `{instance_id}` · answered by **{runner}** "
        f"({type(runners[runner]).__name__}) · "
        f"grants: {', '.join(profile.capabilities) or 'none yet'}"
    )
    with st.expander("Instructions this agent is running with"):
        st.write(profile.instructions)

    _draw_messages(asyncio.run(session.history(instance_id)))

    question = st.chat_input(f"Message {agent_id}")
    if not question:
        return

    with st.chat_message("user"):
        st.markdown(question)

    try:
        with st.spinner(f"{agent_id} is working on {runner}…"):
            turn = asyncio.run(session.say(instance_id, question, runner=runner))
    except Exception as error:  # noqa: BLE001 -- the page reports, never crashes
        with st.chat_message("assistant"):
            st.error(f"**{type(error).__name__}**\n\n{error}")
        return

    _draw_messages(turn.transcript)
    st.caption(" · ".join([runner, *_turn_details(turn)]))


def _draw_messages(messages: Sequence[Message]) -> None:
    """Render a conversation, actions included.

    An action is two messages -- the call and its result -- so they are paired
    back up here and drawn as one collapsed block. Expanded by default would
    bury the answer under whatever the agent read to find it.
    """
    results = {m.tool_call_id: m.content for m in messages if m.role is Role.TOOL}

    for message in messages:
        if message.role is Role.TOOL:
            continue  # drawn with the call that asked for it
        if message.tool_calls:
            for call in message.tool_calls:
                with st.chat_message("assistant", avatar="🔧"):
                    with st.expander(f"ran `{call.name}`"):
                        st.code(call.arguments, language="json")
                        st.code(results.get(call.call_id, "(no output)"))
            continue
        avatar = CHAT_ROLES.get(message.role)
        if avatar and message.content:
            with st.chat_message(avatar):
                st.markdown(message.content)


def _turn_details(turn: AgentTurn) -> list[str]:
    """The footnote under a reply: how it ended, and what it cost.

    Cached tokens are shown because they are the difference between a prompt
    that is merely long and one being re-billed in full on every turn.
    """
    details = [f"finished: `{turn.finish_reason.value}`"]
    if turn.steps:
        details.append(f"{len(turn.steps)} steps recorded")
    if turn.usage:
        details.append(
            f"tokens: {turn.usage.prompt_tokens} in "
            f"({turn.usage.cached_prompt_tokens} cached) / "
            f"{turn.usage.completion_tokens} out"
        )
        if turn.usage.cost_usd is not None:
            details.append(f"cost: ${turn.usage.cost_usd:.4f}")
    return details


# --- run pages (unwired) ----------------------------------------------------


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
            "No runs to show. Nothing records runs yet -- turn on **Demo data** in "
            "the sidebar to see the layout with rows in it."
        )
        return

    st.dataframe(
        [
            {
                "run": run.run_id,
                "task": run.task_id,
                "phase": run.phase.value,
                "repository": run.repository,
                "agent runs": run.agent_runs,
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
    detail[2].metric("Agent runs", run.agent_runs)
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


def wiring_page(capabilities: Capabilities, runners: Mapping[str, AgentRunner]) -> None:
    st.title("Wiring")
    st.caption(
        "What this process composed at startup: one implementation per port, "
        "chosen in `composition.py` and nowhere else. Reading it is introspection "
        "-- no capability is called."
    )

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
        "Two of these work: the agent runner shells out to a coding CLI, and the "
        "state store keeps conversations in memory for as long as this process "
        "lives. The rest are placeholders whose methods raise."
    )

    st.subheader("Runners")
    st.caption(
        "A port has one implementation, and that is the `agent_runner` above -- "
        "what anything non-interactive uses. Chat additionally offers a choice, "
        "bound to these names in `composition.build_runners` and nowhere else."
    )
    st.dataframe(
        [
            {
                "name": name,
                "implementation": type(runner).__name__,
                "module": type(runner).__module__,
                "default": name == next(iter(runners)),
            }
            for name, runner in runners.items()
        ],
        hide_index=True,
    )


# --- shell ------------------------------------------------------------------


def main() -> None:
    capabilities, runners, session = wiring(SETTINGS)

    st.sidebar.title("engine")
    st.sidebar.caption("control interface")
    page = st.sidebar.radio("Page", PAGES, label_visibility="collapsed")

    if page == "Chat":
        st.sidebar.divider()
        st.sidebar.success("Chat is live. Conversations are lost when this process stops.")
        chat_page(session, runners)
        return

    if page == "Wiring":
        wiring_page(capabilities, runners)
        return

    demo = st.sidebar.toggle(
        "Demo data",
        value=False,
        help="Fill the run pages with fixed, invented rows so the layout can be judged.",
    )
    st.sidebar.divider()
    if demo:
        st.sidebar.warning("Showing demo data. None of it came from a running engine.")
    else:
        st.sidebar.info("Not wired: no runs are being recorded yet.")

    model: ReadModel = DemoReadModel() if demo else build_read_model(SETTINGS)
    {"Runs": runs_page, "Request a run": request_page, "Inbox": inbox_page}[page](model)


main()
