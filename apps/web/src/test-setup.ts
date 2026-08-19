import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom lays nothing out, so it implements neither of the two things the
// thread viewport does on sight: measure itself, and scroll to the latest turn.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;
Element.prototype.scrollTo ??= () => {};

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});
