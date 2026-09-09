import { act, render, screen, waitFor } from "@testing-library/react";
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

afterEach(() => vi.unstubAllGlobals());

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
    expect(screen.getByText("octocat")).toBeVisible();
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
