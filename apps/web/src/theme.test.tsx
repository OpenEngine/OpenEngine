import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { RailFoot } from "./brand";
import { applyTheme, storedTheme } from "./theme";

describe("theme toggle", () => {
  afterEach(() => {
    delete document.documentElement.dataset.theme;
  });

  it("switches to dark and back, and remembers the choice", async () => {
    applyTheme(storedTheme());
    render(<RailFoot />);
    expect(document.documentElement.dataset.theme).toBe("light");

    await userEvent.click(screen.getByRole("button", { name: "Switch to dark mode" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(window.localStorage.getItem("engine.theme")).toBe("dark");
    expect(storedTheme()).toBe("dark");

    await userEvent.click(screen.getByRole("button", { name: "Switch to light mode" }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(storedTheme()).toBe("light");
  });
});
