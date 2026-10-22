"""CI runs the root `e2e/` Playwright suite on pull requests (issue #833).

The suite drives the `oe` CLI through node-pty, a WebSocket and xterm.
It is a different suite from `apps/web/e2e`, which the `browser` job in
`tests.yml` already runs, so these checks look for a job that works in
`e2e/` itself, in whichever workflow file it lives.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(path):
    workflow = yaml.safe_load(path.read_text())
    # YAML 1.1 reads the bare key `on` as the boolean true.
    if True in workflow:
        workflow["on"] = workflow.pop(True)
    return workflow


def _steps_in_e2e(job):
    """Each step's command, with the directory it runs in resolved."""
    default = job.get("defaults", {}).get("run", {}).get("working-directory", ".")
    for step in job.get("steps", []):
        directory = step.get("working-directory", default)
        yield step, directory.strip("./") or ".", step.get("run", "")


def _runs_in_e2e(directory, command, pattern):
    """Whether `command` matches `pattern` and runs against the root `e2e/`."""
    if not re.search(pattern, command):
        return False
    return directory == "e2e" or re.search(r"(--prefix[ =]|cd )\.?/?e2e\b", command)


def _cli_e2e_jobs():
    found = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        workflow = _load(path)
        for name, job in workflow.get("jobs", {}).items():
            if any(
                _runs_in_e2e(directory, command, r"\bnpm\b.*\b(test|run test)\b")
                for _, directory, command in _steps_in_e2e(job)
            ):
                found.append((path, workflow, name, job))
    return found


def _the_cli_e2e_job():
    jobs = _cli_e2e_jobs()
    assert jobs, (
        "no workflow job runs `npm test` in the root e2e/ directory; the "
        "existing `browser` job only covers apps/web"
    )
    assert len(jobs) == 1, [f"{path.name}:{name}" for path, _, name, _ in jobs]
    return jobs[0]


def _step_index(job, predicate):
    for index, step in enumerate(job["steps"]):
        if predicate(step):
            return index
    return None


def _uses(prefix):
    return lambda step: step.get("uses", "").startswith(prefix)


def _runs(pattern):
    def predicate(step):
        return re.search(pattern, step.get("run", "")) is not None
    return predicate


def _the_test_step(job):
    index = _step_index(
        job,
        lambda step: any(
            _runs_in_e2e(directory, command, r"\bnpm\b.*\b(test|run test)\b")
            for s, directory, command in _steps_in_e2e(job) if s is step
        ),
    )
    assert index is not None
    return index


def test_the_cli_e2e_suite_runs_on_pull_requests():
    _, workflow, _, _ = _the_cli_e2e_job()
    triggers = workflow["on"]
    if isinstance(triggers, str):
        triggers = [triggers]
    assert "pull_request" in triggers


def test_the_suite_runs_on_linux():
    # The suite was only ever verified on macOS; CI is its first Linux run.
    _, _, _, job = _the_cli_e2e_job()
    assert str(job.get("runs-on", "")).startswith("ubuntu-"), job.get("runs-on")


def test_the_job_is_not_the_apps_web_suite():
    _, _, _, job = _the_cli_e2e_job()
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "apps/web" not in commands


def test_the_job_installs_uv_and_the_python_workspace():
    _, _, _, job = _the_cli_e2e_job()
    uv = _step_index(job, _uses("astral-sh/setup-uv"))
    assert uv is not None, "the job never installs uv"
    sync = _step_index(job, _runs(r"\buv sync\b.*--all-packages"))
    assert sync is not None, "the job never installs the Python workspace"
    # Without --locked a stale uv.lock is silently re-resolved instead of failing.
    assert "--locked" in job["steps"][sync]["run"]
    assert uv < sync < _the_test_step(job)


def test_the_job_installs_a_node_that_satisfies_e2e():
    _, _, _, job = _the_cli_e2e_job()
    index = _step_index(job, _uses("actions/setup-node"))
    assert index is not None, "the job never installs Node"
    version = str(job["steps"][index].get("with", {}).get("node-version", ""))
    # e2e/package.json requires >=22.18, so a bare "22" or an older line is
    # not enough; "lts/*" or a newer major is.
    match = re.fullmatch(r"(\d+)(?:\.(\d+))?(?:\.\d+)?(?:\.x)?", version)
    if match:
        major, minor = int(match[1]), int(match[2] or 0)
        assert (major, minor) >= (22, 18) and (major > 22 or match[2]), version
    else:
        assert version in {"lts/*", "latest", "node"}, version
    assert index < _the_test_step(job)


def test_the_job_installs_the_e2e_dependencies_with_npm_ci():
    _, _, job_name, job = _the_cli_e2e_job()
    index = next(
        (
            i for i, (_, directory, command) in enumerate(_steps_in_e2e(job))
            if _runs_in_e2e(directory, command, r"\bnpm\b.*\bci\b")
        ),
        None,
    )
    assert index is not None, f"{job_name} never runs `npm ci` in e2e/"
    # `npm ci` runs postinstall, which rebuilds node-pty; skipping scripts
    # would leave the terminal server unable to start.
    assert "--ignore-scripts" not in job["steps"][index]["run"]
    assert index < _the_test_step(job)


def test_the_job_installs_chromium_with_its_system_deps():
    _, _, _, job = _the_cli_e2e_job()
    index = _step_index(job, _runs(r"playwright install\b.*--with-deps.*\bchromium\b"))
    if index is None:
        index = _step_index(job, _runs(r"playwright install\b.*\bchromium\b.*--with-deps"))
    assert index is not None, "the job never installs Playwright Chromium --with-deps"
    command = job["steps"][index]["run"]
    directory = job["steps"][index].get(
        "working-directory",
        job.get("defaults", {}).get("run", {}).get("working-directory", "."),
    )
    # The browser has to match e2e/'s Playwright, not apps/web's.
    assert "apps/web" not in command
    assert directory.strip("./") == "e2e" or re.search(r"(--prefix[ =]|cd )\.?/?e2e\b", command)
    assert index < _the_test_step(job)


def test_the_suite_runs_with_scripted_agents():
    _, workflow, _, job = _the_cli_e2e_job()
    step = job["steps"][_the_test_step(job)]
    env = {
        **(workflow.get("env") or {}),
        **(job.get("env") or {}),
        **(step.get("env") or {}),
    }
    assert env.get("E2E_AGENT") == "stub" or re.search(
        r"\bE2E_AGENT=stub\b", step["run"]
    )


def _upload(job, path, when):
    for step in job["steps"]:
        if not step.get("uses", "").startswith("actions/upload-artifact"):
            continue
        uploaded = str(step.get("with", {}).get("path", ""))
        if path not in [line.strip().rstrip("/") for line in uploaded.splitlines()]:
            continue
        if step.get("if", "").replace("${{", "").replace("}}", "").strip() in when:
            return step
    return None


def test_a_failed_run_uploads_the_playwright_report():
    _, _, _, job = _the_cli_e2e_job()
    step = _upload(job, "e2e/playwright-report", {"failure()", "always()"})
    assert step is not None, "no artifact upload of e2e/playwright-report on failure"


def test_a_failed_run_uploads_the_traces():
    _, _, _, job = _the_cli_e2e_job()
    step = _upload(job, "e2e/test-results", {"failure()", "always()"})
    assert step is not None, "no artifact upload of e2e/test-results on failure"


def test_uploads_do_not_clash_with_the_apps_web_artifacts():
    path, _, _, job = _the_cli_e2e_job()
    names = {
        step["with"]["name"]
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact")
    }
    if path.name == "tests.yml":
        # Artifact names are unique per workflow run.
        assert not names & {"playwright-report", "playwright-traces"}, names


def test_a_path_filter_still_covers_everything_the_suite_drives():
    _, workflow, _, _ = _the_cli_e2e_job()
    pull_request = workflow["on"]["pull_request"] if isinstance(workflow["on"], dict) else None
    paths = (pull_request or {}).get("paths")
    if paths is None:
        # No filter: the job runs on every pull request, which is allowed.
        return
    for directory in ("e2e", "cli", "apps", "packages", "migrations"):
        assert any(
            pattern.rstrip("*/") == directory for pattern in paths
        ), f"a change under {directory}/ would not run the CLI e2e suite: {paths}"


def test_a_failed_ci_run_leaves_a_report_and_traces_to_upload():
    # The uploads above only help if the suite writes what they collect: the
    # html reporter to playwright-report, and traces of retried failures to
    # test-results.
    config = (ROOT / "e2e" / "playwright.config.ts").read_text()
    assert re.search(r"reporter:[^\n]*process\.env\.CI[^\n]*\"html\"", config), config
    assert re.search(r"retries:\s*process\.env\.CI\s*\?\s*[1-9]", config)
    assert re.search(r'trace:\s*"(on-first-retry|retain-on-failure|on-all-retries|on)"', config)
