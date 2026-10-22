/** What a long name in a conversation's header must not do: take the screen.
 *
 *  A run started from an issue brief or a plain instruction is named by it, and
 *  those run to hundreds of characters. The heading shows a few lines of it and
 *  carries the whole of it in its title; the chat under it stays where a person
 *  can read it and write into it. Shared by the graph node's conversation and
 *  the chat, which draw their headers from the same `.panel-head`. */

import type { Page } from "@playwright/test";

import { expect } from "./harness";

/** As long as the longest name the issue that asked for this saw (573). */
export const LONG_NAME = [
  "#978 The conversation header on a graph node's page grows without limit",
  "when the WorkOrder name is long, because runs started from an issue brief",
  "or from a plain instruction are named by the whole of that text, and the",
  "heading that shows it has no height limit while the panel gives the header",
  "row an automatic height, so the row grows with the heading until the message",
  "list and the composer under it are pushed below the bottom of the window and",
  "the person reading the conversation cannot see what the agent said or answer",
  "it without scrolling a page that was never meant to scroll at all",
].join(" ");

/** The heading names the conversation in full, and shows about three lines. */
export async function expectClampedHeading(page: Page, name: string): Promise<void> {
  const heading = page.locator(".panel-head h1");
  // Soft, both of these: a page that gets one of them wrong should still be
  // asked about the rest, down to whether the chat is on screen.
  await expect.soft(heading).toHaveAttribute("title", name);
  const { height, lineHeight } = await heading.evaluate((element) => ({
    height: element.getBoundingClientRect().height,
    lineHeight: parseFloat(getComputedStyle(element).lineHeight),
  }));
  // A pixel of slack for sub-pixel line boxes; a fourth line is a whole one.
  expect.soft(height, "the heading's height, in pixels").toBeLessThanOrEqual(3 * lineHeight + 1);
}

/** The message list and the composer are both on screen. */
export async function expectChatOnScreen(page: Page): Promise<void> {
  // What a person writes in and presses, whole. Not the composer's own box:
  // the message list's top edge already trims a sliver of its padding.
  await expect(page.getByLabel("Message the agent")).toBeInViewport({ ratio: 1 });
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeInViewport({ ratio: 1 });
  const stream = page.locator(".stream");
  await expect(stream).toBeInViewport();
  // Not a sliver of it: enough of the list to read a message in.
  const visible = await stream.evaluate((element) => {
    const box = element.getBoundingClientRect();
    return Math.min(box.bottom, window.innerHeight) - Math.max(box.top, 0);
  });
  expect(visible, "how much of the message list is on screen, in pixels")
    .toBeGreaterThanOrEqual(120);
}
