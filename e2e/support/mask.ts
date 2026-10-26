import type { Locator } from "@playwright/test"

/** Opaque, so nothing of what's underneath shows through. */
export const MASK_COLOR = "#000000"

/**
 * A secret on screen (a token, a webhook secret) goes in a screenshot behind a
 * solid box. Pass it among the targets of `screenshot()` from test2doc:
 *
 *     await screenshot(testInfo, [mask(page.getByTestId("token")), { target: other }])
 *
 * The box covers the target's whole bounding box, so give a value its own
 * element rather than masking a paragraph that contains it.
 */
export function mask(target: Locator) {
  return {
    target,
    options: {
      annotation: { highlightFillStyle: MASK_COLOR, highlightStrokeStyle: MASK_COLOR, highlightLineWidth: 4 },
    },
  }
}
