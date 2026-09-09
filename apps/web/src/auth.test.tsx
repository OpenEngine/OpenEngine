import { useState } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getAuthStatus, logout, type AuthStatus } from "./api";
import { AuthGate } from "./auth";

vi.mock("./api", () => ({
  getAuthStatus: vi.fn(),
  logout: vi.fn(),
}));

const signedOut: AuthStatus = {
  loginRequired: true, authenticated: false, user: null,
};
const signedIn: AuthStatus = {
  loginRequired: true, authenticated: true, user: { id: 42, login: "octocat" },
};
const replace = vi.fn();

function visit(path: string) {
  const url = new URL(path, "http://localhost");
  vi.stubGlobal("window", {
    ...window,
    document: window.document,
    location: { pathname: url.pathname, search: url.search, replace },
  });
}

function renderGate() {
  const appMounted = vi.fn();
  function App() {
    appMounted();
    return <div>Protected application</div>;
  }
  render(<AuthGate><App /></AuthGate>);
  return appMounted;
}

beforeEach(() => {
  vi.resetAllMocks();
  visit("/");
});

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("AuthGate", () => {
  it("keeps the application unmounted until the status check finishes", async () => {
    let resolve!: (status: AuthStatus) => void;
    vi.mocked(getAuthStatus).mockReturnValue(new Promise((done) => { resolve = done; }));
    const appMounted = renderGate();
    expect(screen.getByText("Starting openengine…")).toBeVisible();
    expect(appMounted).not.toHaveBeenCalled();

    await act(async () => resolve(signedOut));
    expect(screen.getByRole("link", { name: "Sign in with GitHub" }))
      .toHaveAttribute("href", "/api/auth/github/login");
    expect(appMounted).not.toHaveBeenCalled();
  });

  it.each(["/", "/login", "/runs"])("shows login for a signed-out user at %s", async (path) => {
    visit(path);
    vi.mocked(getAuthStatus).mockResolvedValue(signedOut);
    const appMounted = renderGate();
    expect(await screen.findByRole("link", { name: "Sign in with GitHub" })).toBeVisible();
    expect(appMounted).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });

  it("opens the app and displays the authenticated user's name", async () => {
    vi.mocked(getAuthStatus).mockResolvedValue(signedIn);
    renderGate();
    expect(await screen.findByText("Protected application")).toBeVisible();
    expect(screen.getByRole("group", { name: "Signed in as octocat" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeVisible();
  });

  it("opens the app without a user badge when login is not configured", async () => {
    vi.mocked(getAuthStatus).mockResolvedValue({ ...signedOut, loginRequired: false });
    renderGate();
    expect(await screen.findByText("Protected application")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Sign in with GitHub" })).not.toBeInTheDocument();
  });

  it.each([signedIn, { ...signedOut, loginRequired: false }])(
    "redirects away from /login when the app is accessible (%j)",
    async (status) => {
      visit("/login");
      vi.mocked(getAuthStatus).mockResolvedValue(status);
      const appMounted = renderGate();
      await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
      expect(screen.getByText("Redirecting…")).toBeVisible();
      expect(appMounted).not.toHaveBeenCalled();
      expect(screen.queryByRole("link")).not.toBeInTheDocument();
    },
  );

  it("shows a connection error without opening the app or offering login on status failure", async () => {
    vi.mocked(getAuthStatus).mockRejectedValue(new Error("Network unavailable"));
    const appMounted = renderGate();
    expect(await screen.findByText("Could not reach the server. Please refresh to try again.")).toBeVisible();
    expect(appMounted).not.toHaveBeenCalled();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it("waits for logout to finish before navigating to login", async () => {
    vi.mocked(getAuthStatus).mockResolvedValue(signedIn);
    let resolve!: () => void;
    vi.mocked(logout).mockReturnValue(new Promise<void>((done) => { resolve = done; }));
    renderGate();
    const button = await screen.findByRole("button", { name: "Sign out" });
    await userEvent.setup().click(button);
    expect(logout).toHaveBeenCalledTimes(1);
    expect(replace).not.toHaveBeenCalled();
    await act(async () => resolve());
    expect(replace).toHaveBeenCalledWith("/login");
  });

  it("unmounts the app when a session expires while the page stays open", async () => {
    vi.useFakeTimers();
    vi.mocked(getAuthStatus).mockResolvedValueOnce(signedIn).mockResolvedValue(signedOut);
    renderGate();
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText("Protected application")).toBeVisible();
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(screen.queryByText("Protected application")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in with GitHub" })).toBeVisible();
  });

  it.each([signedIn, { ...signedOut, loginRequired: false }])(
    "preserves edited application state across a failed background check (%j)",
    async (status) => {
      vi.useFakeTimers();
      vi.mocked(getAuthStatus).mockResolvedValueOnce(status)
        .mockRejectedValueOnce(new Error("Temporary outage"))
        .mockResolvedValue(status);
      function Editor() {
        const [note, setNote] = useState("");
        return <input aria-label="Decision note" value={note}
          onChange={(event) => setNote(event.target.value)} />;
      }
      render(<AuthGate><Editor /></AuthGate>);
      await act(async () => { await Promise.resolve(); });
      const input = screen.getByRole("textbox", { name: "Decision note" });
      fireEvent.change(input, { target: { value: "Keep this draft" } });
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
      expect(input).toBeVisible();
      expect(input).toHaveValue("Keep this draft");
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
      expect(getAuthStatus).toHaveBeenCalledTimes(3);
      expect(screen.getByRole("textbox", { name: "Decision note" })).toBe(input);
      expect(input).toHaveValue("Keep this draft");
    },
  );

  it("shows sign-out failure and allows retry without navigating early", async () => {
    vi.mocked(getAuthStatus).mockResolvedValue(signedIn);
    vi.mocked(logout).mockRejectedValueOnce(new Error("Network failure")).mockResolvedValue(undefined);
    renderGate();
    const button = await screen.findByRole("button", { name: "Sign out" });
    const user = userEvent.setup();
    await user.click(button);
    expect(screen.getByRole("alert")).toHaveTextContent("Could not sign out");
    expect(replace).not.toHaveBeenCalled();
    await user.click(button);
    expect(replace).toHaveBeenCalledWith("/login");
  });

  it.each([
    ["expired", "Login expired. Please try again."],
    ["denied", "GitHub authorization was not completed."],
    ["failed", "Could not verify your GitHub identity. Please try again."],
    ["unknown", "Could not verify your GitHub identity. Please try again."],
  ])("explains the %s login error", async (error, message) => {
    visit("/login?error=" + error);
    vi.mocked(getAuthStatus).mockResolvedValue(signedOut);
    renderGate();
    expect(await screen.findByText(message)).toBeVisible();
    expect(screen.getByRole("link", { name: "Sign in with GitHub" })).toBeVisible();
  });

  it("does not show an OAuth error when no error parameter is present", async () => {
    visit("/login");
    vi.mocked(getAuthStatus).mockResolvedValue(signedOut);
    renderGate();
    await screen.findByRole("link", { name: "Sign in with GitHub" });
    expect(screen.queryByText(/expired|not completed|Could not verify/)).not.toBeInTheDocument();
  });
});
