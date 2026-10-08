"""Small evidence-led summaries for reports and published comments."""


def brief(value: str, *, words=24, chars=180) -> str:
    """Bound display text while preserving the complete evidence in details."""
    text = " ".join(value.split())
    shortened = " ".join(text.split()[:words])
    if len(shortened) > chars:
        shortened = shortened[:chars].rsplit(" ", 1)[0]
    return shortened + ("…" if shortened != text else "")


def observed_summary(detail: str, checkpoints: list[dict], status: str) -> list[tuple[str, str]]:
    """Show executed checks, prioritizing problems and counting unexecuted checks."""
    observed = [p for p in checkpoints if p.get("code") != "NOT_RUN"]
    problems = [p for p in observed if p["status"] != "passed"]
    diagnoses = {'FIXTURE_ERROR', 'ASSERTION_INVALID', 'UNEXPECTED_APP_ERROR', 'APP_HEALTH_INCONCLUSIVE', 'QA_ACTION_ERROR'}
    problems.sort(key=lambda p: (p.get('code') == 'APP_HEALTH_INCONCLUSIVE',
                                 p.get('code') not in diagnoses))
    passed = [p for p in observed if p["status"] == "passed"]
    selected = (problems + passed)[:3]
    rows = []
    for point in selected:
        label = "inconclusive" if point.get("code") == "ASSERTION_INCONCLUSIVE" else point["status"]
        text = point["instruction"]
        if point.get("code") in diagnoses | {"HARNESS_ERROR"}:
            rows.append((label, brief(point["detail"])))
            continue
        if point.get("detail") and point["detail"] not in {
            "Exact assertion passed", "Mapped operation assertions passed"
        }:
            text += ": " + point["detail"]
        rows.append((label, brief(text)))
    if not observed:
        rows.append((status, brief(detail) or "No execution details recorded."))
    if len(observed) > len(selected):
        rows.append(("details", f"{len(observed) - len(selected)} more executed checks"))
    not_run = len(checkpoints) - len(observed)
    if not_run:
        rows.append(("not run", f"{not_run} remaining checks"))
    return rows


def evidence_caption(detail: str, checkpoints: list[dict], status: str) -> list[tuple[str, str]]:
    """Caption actual evidence in one short row; retain blockers and unexecuted counts."""
    observed = [p for p in checkpoints if p.get('code') != 'NOT_RUN']
    problems = [p for p in observed if p['status'] != 'passed']
    not_run = len(checkpoints) - len(observed)
    if problems or status != 'passed':
        rows = observed_summary(detail, problems, status)[:1]
        passed = [p for p in observed if p['status'] == 'passed'
                  and not p['instruction'].startswith('Host:')]
        if passed:
            rows.append(('passed', ' → '.join(brief(p['instruction'], words=8, chars=65)
                                             for p in passed[-3:])))
    elif observed:
        product = [p for p in observed if not p['instruction'].startswith('Host:')]
        example = (product or observed)[-1]['instruction']
        rows = [('passed', f"{len(observed)} {'check' if len(observed) == 1 else 'checks'}. {brief(example, words=16, chars=110)}")]
    else:
        rows = [(status, brief(detail, words=20, chars=140) or 'No execution details recorded.')]
    if not_run:
        rows.append(('not run', f"{not_run} {'check' if not_run == 1 else 'checks'}"))
    return rows
