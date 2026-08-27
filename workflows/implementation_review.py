"""Repository-owned implementation, review, and human decision workflow."""

import openengine as oe


implementation_agent = oe.agent(
    id="implementation-agent",
    instructions=(
        "Implement the requested change in the provided workspace. Read the code "
        "before editing. Engine has already based the workspace on the current remote "
        "main commit; do not fetch, pull, or merge main before editing. Make the "
        "smallest complete change and report the result.\n\n"
        "Every git operation goes through the git_subcommand tool, which runs git "
        "in this workspace -- not through the shell, which is not permitted to run "
        "git here. Any subcommand is available to it. When the change is ready: "
        "create a descriptive branch such as agent/<description> from the base, "
        "commit only the work for this change, push the branch, and then call "
        "open_pull_request and report the URL it returns as the pr_url output. "
        "The workspace's own engine/ branch is Engine's bookkeeping and cannot be "
        "pushed."
    ),
    capabilities=["git_subcommand", "open_pull_request"],
    description="Implements the requested repository change.",
)

naming_agent = oe.agent(
    id="implementation-agent",
    instructions=(
        "Give a submitted workflow a concise display name. Do not inspect or change "
        "the workspace and do not perform the requested task."
    ),
    description="Names a submitted implementation workflow.",
)

review_agent = oe.agent(
    id="review-agent",
    instructions=(
        "Review the implementation already made in the provided workspace. Read the "
        "changed code and the code around it before judging it, and check "
        "correctness, regressions the change could cause, and tests that should "
        "exist but do not. Inspect the workspace only: do not edit, revert, "
        "commit, or otherwise modify anything, and do not fix what you find. "
        "Report every finding with the file it is in and why it matters, and say "
        "so explicitly when you find nothing. Leave every finding on the pull "
        "request with the add_comment MCP tool, using file and line for inline "
        "comments when possible; if there are no findings, leave one general "
        "comment saying so. Complete the step with your findings "
        "even when they are serious; fail it only when the review itself could not "
        "be carried out. The human reviewer decides what happens to this run -- you "
        "do not approve or reject it."
    ),
    capabilities=["add_comment"],
    description="Inspects an implementation without modifying it.",
)

implementation = oe.result("implementation")
review = oe.result("review")

workflow = oe.workflow(
    id="implementation-review-v1",
    name="Implementation review",
    version="v1",
    workspace=oe.workspace(),
    naming_agent=naming_agent,
    naming_prompt=(
        "Name this workflow based on the task above. Do not perform the task or use "
        "tools. Reply with only a concise name of at most eight words, with no quotes "
        "or ending punctuation."
    ),
    steps=[
        oe.agent_step(
            id="implementation",
            name="Implementation",
            agent=implementation_agent,
            prompt=oe.template("{task}", task=oe.task.prompt),
            required_outputs=["pr_url"],
            workspace_access="write",
            editable=True,
            transitions={
                "success": oe.goto("review"),
                "*": oe.fail(),
            },
        ),
        oe.agent_step(
            id="review",
            name="Review",
            agent=review_agent,
            prompt=oe.template(
                "Inspect the workspace but do not modify it. Review the implementation "
                "below against the original task and report what a human deciding this "
                "run needs to know, in the `findings` output.\n\n"
                "Original task:\n{task}\n\n"
                "Implementation result:\n"
                "Outcome: {implementation_outcome}\n"
                "Summary: {implementation_summary}\n"
                "Outputs:\n{implementation_outputs}",
                task=oe.task.prompt,
                implementation_outcome=implementation.outcome,
                implementation_summary=implementation.summary,
                implementation_outputs=implementation.outputs,
            ),
            required_outputs=["findings"],
            workspace_access="read",
            transitions={"*": oe.goto("human-review")},
        ),
        oe.human_review_step(
            id="human-review",
            name="Human review",
            title=oe.template("Review implementation for {task_id}", task_id=oe.task.id),
            summary=oe.template(
                "Implementation:\n{implementation_summary}\n"
                "Outputs:\n{implementation_outputs}\n\n"
                "Review:\n"
                "Outcome: {review_outcome}\n"
                "{review_summary}\n"
                "Outputs:\n{review_outputs}",
                implementation_summary=implementation.summary,
                implementation_outputs=implementation.outputs,
                review_outcome=review.outcome,
                review_summary=review.summary,
                review_outputs=review.outputs,
            ),
            approved=oe.succeed(),
            rejected=oe.fail(),
        ),
    ],
)
