import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import { SettingsPanel } from "./settings-panel";

vi.mock("./api", () => ({
  connectGitHub: vi.fn(),
  disconnectGitHub: vi.fn(),
  getGitHubClientId: vi.fn(),
  getGitHubStatus: vi.fn(),
  getSourceControlStatus: vi.fn(),
  pollGitHubConnect: vi.fn(),
  setGitHubClientId: vi.fn(),
  setSourceControlProvider: vi.fn(),
  connectSlack: vi.fn(),
  disconnectSlack: vi.fn(),
  getSlackStatus: vi.fn(),
  setSlackCredentials: vi.fn(),
}));

describe("SettingsPanel Slack connection", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: { clear: vi.fn() },
    });
    vi.mocked(api.getGitHubClientId).mockResolvedValue({ source: "none", hint: "" });
    vi.mocked(api.getGitHubStatus).mockResolvedValue({ connected: false, clientIdConfigured: false });
    vi.mocked(api.getSourceControlStatus).mockResolvedValue({
      provider: "gh-cli",
      autoSelected: false,
      ghCli: { installed: true, authenticated: true, account: "", message: "" },
    });
    vi.mocked(api.connectSlack).mockResolvedValue({ authorizationUrl: "https://slack.example/oauth" });
    vi.spyOn(window, "open").mockImplementation(() => null);
  });

  it("shows an error when polling Slack status fails", async () => {
    vi.mocked(api.getSlackStatus)
      .mockResolvedValueOnce({ configured: true, connected: false })
      .mockRejectedValueOnce(new Error("Slack status unavailable"));

    render(<SettingsPanel onClose={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Connect Slack" }));

    await waitFor(() => expect(screen.getByText("Slack status unavailable")).toBeVisible());
    expect(screen.queryByText("Checking…")).not.toBeInTheDocument();
  });

  it("shows an error when Slack token revocation fails", async () => {
    vi.mocked(api.getSlackStatus).mockResolvedValue({ configured: true, connected: true });
    vi.mocked(api.disconnectSlack).mockRejectedValue(new Error("Slack revocation failed"));

    render(<SettingsPanel onClose={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Disconnect" }));

    await waitFor(() => expect(screen.getByText("Slack revocation failed")).toBeVisible());
    expect(screen.getByText("Connected")).toBeVisible();
    expect(screen.queryByText("Checking…")).not.toBeInTheDocument();
  });

  it("refreshes connection status when saving new credentials fails", async () => {
    vi.mocked(api.getSlackStatus)
      .mockResolvedValueOnce({ configured: true, connected: true })
      .mockResolvedValueOnce({ configured: true, connected: false });
    vi.mocked(api.setSlackCredentials).mockRejectedValue(new Error("Could not save credentials"));

    render(<SettingsPanel onClose={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Change credentials" }));
    await user.type(screen.getByLabelText("Slack OAuth Client ID"), "new-client");
    await user.type(screen.getByLabelText("Slack OAuth Client Secret"), "new-secret");
    await user.click(screen.getByRole("button", { name: "Save credentials" }));

    await waitFor(() => expect(screen.getByText("Could not save credentials")).toBeVisible());
    expect(api.getSlackStatus).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("Connected")).not.toBeInTheDocument();
  });
});
