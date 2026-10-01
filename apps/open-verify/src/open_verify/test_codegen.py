"""Compile typed journeys into ordinary, editable Playwright Python tests."""

from open_verify.test_spec import BrowserTest, Locator


def locator_code(locator: Locator) -> str:
    if locator.by == "role":
        return f"page.get_by_role({locator.role!r}, name={locator.name!r}, exact=True)"
    if locator.by == "test_id":
        return f"page.get_by_test_id({locator.name!r})"
    return f"page.get_by_{locator.by}({locator.name!r}, exact=True)"


def render_test(test: BrowserTest, *, login: dict | None = None) -> str:
    # Only this compiler supplies Python syntax. All model-provided strings are literals.
    lines = [
        '"""Generated Playwright journey. Start the app before running this file."""',
        "from pathlib import Path",
        "from playwright.async_api import expect",
        "",
        "",
        "async def test_change(page, *, entry_url=None, progress=print):",
        f"    await page.goto(entry_url or {test.url!r}, "
        "wait_until='domcontentloaded', timeout=30000)",
    ]
    for index, step in enumerate(test.steps):
        labels = {
            "reload": "Reloading the app to check session persistence",
            "navigate": "Opening an app page",
            "expect_json": "Checking an API response",
            "screenshot": "Capturing a screenshot",
            "click": "Clicking a control",
            "fill": "Filling a field",
            "press": "Pressing a key",
            "expect_text": "Checking visible app content",
            "expect_url": "Checking the page URL",
        }
        checks = [name for name, indexes in test.checks.items() if index in indexes]
        label = "; ".join(checks) if checks else labels[step.kind]
        lines.append(f"    progress({f'  Check {index + 1}/{len(test.steps)}: {label}'!r})")
        if step.kind == "click":
            line = f"await {locator_code(step.locator)}.click()"
        elif step.kind == "fill":
            line = f"await {locator_code(step.locator)}.fill({step.value!r})"
        elif step.kind == "press":
            line = f"await {locator_code(step.locator)}.press({step.key!r})"
        elif step.kind == "expect_text":
            assertion = "to_be_visible" if step.visible else "not_to_be_visible"
            line = f"await expect(page.get_by_text({step.text!r}, exact=True)).{assertion}()"
        elif step.kind == "reload":
            line = "await page.reload(wait_until='domcontentloaded')"
        elif step.kind == "navigate":
            line = f"await page.goto(await page.evaluate('(path) => new URL(path, location.origin).href', {step.path!r}), wait_until='domcontentloaded')"
        elif step.kind == "expect_json":
            lines.extend([
                "    response = await page.evaluate(\"\"\"async (path) => {",
                "        const response = await fetch(new URL(path, location.origin), {credentials: 'same-origin', redirect: 'error'});",
                "        return {status: response.status, body: await response.json()};",
                f"    }}\"\"\", {step.path!r})",
                f"    assert response['status'] == {step.status!r}, 'Unexpected API status'",
                "    value = response['body']",
                f"    for key in {step.field!r}:",
                "        value = value[key]",
                f"    assert type(value) is type({step.value!r}) and value == {step.value!r}, 'JSON field did not match expected value'",
            ])
            continue
        elif step.kind == "screenshot":
            lines.append("    if page.video is not None:")
            lines.append("        capture_dir = Path(await page.video.path()).parent")
            lines.append(
                f"        await page.screenshot(path=str(capture_dir / 'checkpoint-{index:02d}-{step.name}.png'))"
            )
            continue
        else:
            line = f"await expect(page).to_have_url({step.url!r})"
        lines.append("    " + line)
        if step.kind == "expect_text" and step.visible:
            lines.append(
                f"    await page.get_by_text({step.text!r}, exact=True).scroll_into_view_if_needed()"
            )
    lines.extend(
        [
            "",
            "",
            "if __name__ == '__main__':",
            "    from open_verify.playwright_runner import replay_main",
            f"    replay_main(test_change, url={test.url!r}, timeout={test.timeout!r}, authenticated={test.authenticated!r}, login={login!r})",
            "",
        ]
    )
    return "\n".join(lines)
