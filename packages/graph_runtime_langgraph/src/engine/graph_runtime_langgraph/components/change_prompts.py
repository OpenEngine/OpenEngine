"""Shared prompts for implementation and review components."""

IMPLEMENTATION_PROMPT = (
    "Implement the requested change in the provided workspace. Read the code "
    "before editing. The workspace is already based on the current commit of the "
    "remote default branch; do not fetch, pull, or merge it before editing. Make the "
    "smallest complete change and report the result.\n\n"
    "{publish}Return the final branch name as the branch output. "
    "Use fail_step "
    "if the work cannot be completed, or clarify only when answering a question "
    "without changing the implementation.\n\n"
    "The task:\n{task}"
)

FACET_REVIEW_PROMPT = (
    "Review the implementation in this workspace, focusing exclusively on "
    "**{facet_name}**.\n\n"
    "{facet_focus}\n\n"
    "The change is {change}. "
    "Read the changed code and the code around it before judging. Inspect "
    "only: do not edit, revert, commit, or modify anything.\n\n"
    "Your acceptance criteria: produce a JSON array of *finding* objects. "
    "Each finding has exactly two required fields:\n"
    '- "tagline": 1-2 lines explaining the issue as you would to a layman\n'
    '- "description": 1-3 followup lines about what it is and why it is bad\n\n'
    "Optional fields (include when applicable):\n"
    '- "file": the file path the finding relates to\n'
    '- "line": the line number within that file\n\n'
    "Call complete_step with the findings output set to the JSON array. "
    "If you find nothing worth reporting, pass an empty array [].\n\n"
    "Original task:\n{task}\n\n"
    "What the implementation reported:\n{implementation}"
)

RERANKER_PROMPT = (
    "You are a senior reviewer consolidating findings from {reviewer_count} "
    "specialized reviewers who each examined the same code change from a "
    "different angle. Your job is to **somewhat aggressively** squash noise.\n\n"
    "Remove any finding that is:\n"
    "- A nitpick or stylistic preference\n"
    "- A duplicate of another finding (even across facets)\n"
    "- About a hypothetical issue that is extremely unlikely in practice\n"
    "- Not actionable -- the author cannot do anything concrete about it\n"
    "- Already handled by existing code the reviewer missed\n\n"
    "Keep findings a senior engineer would genuinely want fixed before "
    "merging, and concrete, actionable findings that are likely real even when "
    "they are minor. When in doubt about one of those, keep it.\n\n"
    "{publishing}"
    "{findings_sections}"
    "{change}"
    "Original task:\n{task}"
)

IMPACT_ANALYSIS_PROMPT = (
    "Assess the impact of the final change {change}. Read the "
    "diff and surrounding code, tests, CI results, and review findings. Inspect "
    "only: do not edit, commit, merge, or deploy anything.\n\n"
    "Rank the change at exactly one level:\n"
    "Green 🟢: No significant UI or architectural changes. Administrative "
    "changes, narrowly scoped bug fixes, or minor product behavior changes. "
    "Well tested with few testing blind spots, and no new integrated component "
    "requiring human setup.\n"
    "Orange 🟠: Moderate UI adjustments, a new architectural component, or "
    "significant code changes to prevent a bug. Well tested, but a new "
    "integrated component may require human setup, or integration testing "
    "may be incomplete for a known reason.\n"
    "Red 🔴: Significant architectural alterations, a major new feature, or "
    "excess complexity. May touch sensitive components such as security or "
    "auth, and requires human involvement to deploy. Requires careful review "
    "and must not be merged without a human.\n\n"
    "Choose the highest applicable level; passing tests alone does not lower "
    "the impact. Explain uncertainty and testing gaps rather than assuming "
    "untested behavior is safe. Give evidence for UI/product and architectural "
    "scope, complexity, sensitive components, test coverage and blind spots, "
    "and human setup or deployment work.\n\n"
    "{publishing}"
    "call complete_step with impact_level set to exactly "
    "Green, Orange, or Red "
    "and impact_rationale containing your evidence and required human actions. "
    "Include the color label and emoji and the rationale in the summary.\n\n"
    "Original task:\n{task}\n\nImplementation report:\n{implementation}\n\n"
    "CI results:\n{ci}\n\nFinal review findings:\n{findings}"
)

#: How a run started in review publishes its consolidated findings: it does not.
KEEP_FINDINGS = (
    "Do not post comments or contact any pull request: the surviving findings "
    "are shown to a person, who chooses which to fix or post. Call "
    "complete_step with the filtered findings as a JSON array (same schema as "
    "the inputs), or [] when none survive. Preserve each finding's agent and "
    "facet fields unchanged.\n\n"
)
