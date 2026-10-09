"""Save replayable typed source without executing it or calling a model."""

import hashlib

from open_verify import __version__
from open_verify.test_codegen import render_test


def save_browser_test(test, artifacts, *, attempt, login=None):
    """Export observed operations; barriers explicitly mark checks requiring a live judge."""
    identity = hashlib.sha256(test.case_id.encode()).hexdigest()[:16]
    folder = artifacts.path / "tests"
    folder.mkdir(exist_ok=True)
    path = folder / f"test_{identity}_{attempt:03d}.py"
    source = render_test(test, login=login)
    path.write_text(source, encoding="utf-8")
    path.with_suffix(".json").write_text(test.model_dump_json(indent=2), encoding="utf-8")
    (folder / "requirements.txt").write_text(
        f"open-verify[browser]=={__version__}\n", encoding="utf-8"
    )
    (folder / "README.md").write_text(
        "# Generated Playwright tests\n\n"
        "Use the same Open Verify version (install from its standalone source if unpublished), "
        "install the browser extra, then run `playwright install chromium`. "
        "Replays containing requires_verification raise an error at that point: a semantic judgment, "
        "unexecuted check, uncertain action or oversized trace needs live verification. "
        "A passed live journey does not imply its export can pass without a model.\n\n"
        "Start the app using ../plan.json and the recorded prerequisites in ../report.md. "
        "Each test starts with an isolated browser context. Authenticated tests use "
        "--login for assisted sign-in or a private --auth-state file for replay; other tests start signed out. Prerequisite "
        "UI steps belong in the test. Use disposable test data. No dependencies are installed automatically.\n\n"
        "Run the manifest's argv from the bundle root. Set OV_BASE_URL to override the entry "
        "URL; absolute navigation destinations and explicit URL assertions keep their recorded values. The test_change(page) "
        "function uses ordinary Playwright and can also be adopted into an async test suite. "
        "The standalone entry point keeps Open Verify's origin guards. "
        "The browser extra includes a GIF encoder; ffmpeg on PATH takes precedence.\n",
        encoding="utf-8",
    )
    if any(folder.glob('test_backend_*.py')):
        from open_verify.backend_runner import write_backend_support
        write_backend_support(folder)
    return path, source
