import type { Page } from "@playwright/test"

/**
 * What the benchmark's verifier (tests/benchmark/verify.spec.ts) uses to drive the calculator the agents built. It knows
 * nothing of how the page is made: keys are the buttons whose visible name is the key's label, and the display is any
 * visible text that is not a key. There are no ids, classes or test ids to depend on, so any markup that shows the keys
 * and the answer passes.
 */

/** How long a key may take to be found or the display to change, in milliseconds. A page that never shows it has failed, not hung. */
const WAIT = 5_000

export const key = (page: Page, label: string) => page.getByRole("button", { name: label, exact: true })

/** Press keys in order, one character a key: `"1+1="` is 1, +, 1 and =. */
export async function press(page: Page, keys: string) {
  for (const label of keys) await key(page, label).click({ timeout: WAIT })
}

/** Wait for the display to read `shows`: more visible text says it than the keys do, since a key labelled `5` is not the display showing 5. */
export async function expectDisplay(page: Page, shows: string) {
  const deadline = Date.now() + WAIT
  while ((await page.getByText(shows, { exact: true }).filter({ visible: true }).count()) <= (await key(page, shows).count())) {
    if (Date.now() > deadline) throw new Error(`expected the display to read ${JSON.stringify(shows)}`)
    await page.waitForTimeout(100)
  }
}
