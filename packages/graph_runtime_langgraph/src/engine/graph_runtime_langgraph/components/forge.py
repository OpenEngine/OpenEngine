"""Prompt wording that differs between connected and disconnected runs.

A workflow's prompts say what to do; *where the result goes* is the part that
changes with the run's mode (see `engine.domain.forge`), and it is the same
part in every workflow: publish the change or commit it here, post the
findings or keep them, name the pull request or the checkout. So those
sentences live here, once, as `ByMode` snippets a prompt template is filled
from, and a workflow never branches on the mode itself.

    IMPLEMENTATION_PROMPT = "Implement the change.\\n\\n{publish}The task:\\n{task}"
    IMPLEMENTATION_PROMPT.format(publish=PUBLISH_CHANGE(state), task=...)

The tools a disconnected run is served are narrowed by `TerminalMcpServer`
and CI by `CICheck`; these snippets are the other half, so the agent is not
told to use a tool it will not be given.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from engine.domain import ForgeMode, forge_mode


def run_mode(state: Mapping[str, object]) -> ForgeMode:
    """The mode of the run whose graph state this is."""
    return forge_mode(state.get("inputs"))


@dataclass(frozen=True, slots=True)
class ByMode:
    """One piece of prompt, worded for each mode.

    Called with the graph state, and with any values its wording names --
    `{pr_url}`, `{runner}` -- which are formatted in.
    """

    connected: str
    disconnected: str

    def for_mode(self, mode: ForgeMode) -> str:
        return self.disconnected if mode is ForgeMode.DISCONNECTED else self.connected

    def __call__(self, state: Mapping[str, object], **values: object) -> str:
        return self.for_mode(run_mode(state)).format(**values)


#: Said to every agent in a disconnected run that could otherwise reach out.
OFFLINE = (
    "This run is disconnected from the forge: do not push, open a pull "
    "request, or post comments anywhere. "
)

#: How a finished change leaves the implementing agent.
PUBLISH_CHANGE = ByMode(
    connected=(
        "Every git operation goes through the git_subcommand tool. When the change "
        "is ready, create a descriptive agent/<description> branch, commit only this "
        "change, push that branch, then call open_pull_request. Finish by calling "
        "complete_step with the URL open_pull_request returned as the pr_url output. "
        "Report that URL, not a pull request number read from an issue, a diff or CI "
        "output. "
    ),
    disconnected=(
        OFFLINE
        + "Every git operation goes through the git_subcommand tool. When the "
        "change is ready, create a descriptive agent/<description> branch and "
        "commit only this change to it in this checkout. Finish by calling "
        "complete_step with a summary of the change and the branch it is on. "
    ),
)

#: Where the change being revised is: `{pr_url}` when connected.
THE_CHANGE = ByMode(
    connected="the existing pull request {pr_url}",
    disconnected="your change in this checkout",
)

#: How a revision to that change is delivered.
UPDATE_CHANGE = ByMode(
    connected=(
        "commit and push to the same PR branch using git_subcommand. "
        "Do not open another pull request. Finish with complete_step and the "
        "same pr_url output. "
    ),
    disconnected=(
        "commit it to the same branch in this checkout using git_subcommand. "
        + OFFLINE
        + "Finish with complete_step summarising what you changed. "
    ),
)

#: What to do about review comments a revision addressed.
ANSWER_REVIEW = ByMode(
    connected=(
        "Reply to each review comment you addressed, explaining the fix, "
        "and resolve its review thread where applicable. "
    ),
    disconnected="",
)

#: What a consolidated review is published as: `{runner}` names the reviewer.
PUBLISH_FINDINGS = ByMode(
    connected=(
        "For each surviving finding, post it as a PR comment using add_comment. "
        "Format each comment as:\n\n"
        "**<tagline>**\n\n"
        "<description>\n\n"
        "_Produced by {runner} reviewing <facet>_\n\n"
        "Use the file and line from the finding for inline comments where "
        "available; use a general comment otherwise. If no findings survive, "
        "leave one general comment saying the change looks clean.\n\n"
        "After posting comments, call complete_step with the filtered findings "
        "as a JSON array (same schema as the inputs). Preserve each finding's "
        "agent and facet fields unchanged.\n\n"
    ),
    disconnected=(
        "This run is disconnected from the forge: do not post comments or contact "
        "any pull request. The surviving findings are shown to a person directly "
        "from your output. Call complete_step with the filtered findings as a "
        "JSON array (same schema as the inputs), or [] when none survive. "
        "Preserve each finding's agent and facet fields unchanged.\n\n"
    ),
)

#: What a single summary -- an assessment, a verdict -- is published as.
#: Ends mid-sentence, before the instruction to complete the step.
PUBLISH_SUMMARY = ByMode(
    connected=(
        "Before completing, use add_comment to post one general comment on pull "
        "request {pr_url} with your results. Include your rationale and "
        "evidence, testing gaps, and required human actions. Then "
    ),
    disconnected=(
        "This run is disconnected from the forge: do not post comments. Your "
        "rationale is shown to a person directly from your output. Then "
    ),
)

#: How a prompt names the change under review: `{pr_url}` when connected.
CHANGE_UNDER_REVIEW = ByMode(
    connected="on pull request {pr_url}",
    disconnected="committed in this checkout",
)


__all__ = [
    "ANSWER_REVIEW",
    "ByMode",
    "CHANGE_UNDER_REVIEW",
    "OFFLINE",
    "PUBLISH_CHANGE",
    "PUBLISH_FINDINGS",
    "PUBLISH_SUMMARY",
    "THE_CHANGE",
    "UPDATE_CHANGE",
    "run_mode",
]
