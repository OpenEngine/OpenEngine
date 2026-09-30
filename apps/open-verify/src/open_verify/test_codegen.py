"""Compile typed journeys into ordinary, editable Playwright Python tests."""

from open_verify.test_spec import BrowserTest, Locator


def locator_code(locator: Locator) -> str:
    if locator.by == "role":
        return f"page.get_by_role({locator.role!r}, name={locator.name!r}, exact=True)"
    if locator.by == "test_id":
        return f"page.get_by_test_id({locator.name!r})"
    return f"page.get_by_{locator.by}({locator.name!r}, exact=True)"


def render_test(test: BrowserTest) -> str:
    # Only this compiler supplies Python syntax. All model-provided strings are literals.
    lines = [
        '"""Generated Playwright journey. Start the app before running this file."""',
        "from playwright.async_api import expect",
        "",
        "",
        "async def test_change(page, *, entry_url=None):",
        f"    await page.goto(entry_url or {test.url!r}, "
        "wait_until='domcontentloaded', timeout=30000)",
    ]
    for step in test.steps:
        if step.kind == "click":
            line = f"await {locator_code(step.locator)}.click()"
        elif step.kind == "fill":
            line = f"await {locator_code(step.locator)}.fill({step.value!r})"
        elif step.kind == "press":
            line = f"await {locator_code(step.locator)}.press({step.key!r})"
        elif step.kind == "expect_text":
            assertion = "to_be_visible" if step.visible else "not_to_be_visible"
            line = f"await expect(page.get_by_text({step.text!r}, exact=True)).{assertion}()"
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
            f"    replay_main(test_change, url={test.url!r}, timeout={test.timeout!r})",
            "",
        ]
    )
    return "\n".join(lines)
