import { test, type Locator, type TestInfo } from "@playwright/test"
import { screenshot } from "@test2doc/playwright/screenshots"
import type { Terminal } from "../fixtures/terminal.ts"

/**
 * Screenshot a terminal down to its last row in use, once it has stopped changing, with
 * `highlight` labelled. The label goes beneath all the output, whichever rows are highlighted.
 */
export async function show(terminal: Terminal, testInfo: TestInfo, highlight: Locator, label: string) {
  await terminal.waitForQuiet()
  const area = await terminal.usedArea()
  const anchor = terminal.page.locator("#guide-label-anchor")
  await terminal.page.evaluate(({ x, y, width, height }) => {
    const element = document.createElement("div")
    element.id = "guide-label-anchor"
    Object.assign(element.style, {
      position: "fixed", left: `${x}px`, top: `${y + height}px`,
      width: `${width}px`, height: "1px", pointerEvents: "none",
    })
    document.body.append(element)
  }, area)
  try {
    await screenshot(testInfo, [
      { target: highlight },
      { target: anchor, options: { annotation: {
        text: label, position: "below", showArrow: false,
        highlightStrokeStyle: "transparent", highlightFillStyle: "transparent",
      } } },
    ], { clip: await terminal.usedArea({ labelBelow: true }) })
  } finally {
    await anchor.evaluate((element) => element.remove())
  }
}

/**
 * A step the page shows as a paragraph of its own (see another-computer.spec.ts).
 * A title starting with `[nodoc]` is left bare: test2doc only skips a title that starts with it.
 */
export function step<T>(text: string, body: () => Promise<T>): Promise<T> {
  return test.step(text.startsWith("[nodoc]") ? text : `\n${text}\n`, body)
}
