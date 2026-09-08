/** Settings panel: GitHub connection and other configuration.
 *
 *  Slides open from the gear icon in the rail foot. The panel lives inside the
 *  rail so it occupies the same column rather than floating over the content. */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  connectGitHub,
  connectGitLab,
  disconnectGitHub,
  disconnectGitLab,
  getGitHubClientId,
  getGitHubStatus,
  getGitLabStatus,
  getSourceControlProvider,
  getSourceControlStatus,
  pollGitHubConnect,
  pollGitLabConnect,
  setGitHubClientId,
  setGitLabClientId,
  setSourceControlProvider,
  connectSlack,
  disconnectSlack,
  getSlackStatus,
  setSlackCredentials,
  setSlackSigningSecret,
  type GitHubClientIdInfo,
  type GitHubConnectResponse,
  type GitLabDeviceFlow,
  type SlackStatus,
} from "./api";

type SlackState = {
  configured: boolean;
  connected: boolean;
  /** Whether pinging the bot can start a work order. */
  events: boolean;
  /** Whether a signing secret is saved -- one of the two halves of `events`. */
  hasSigningSecret: boolean;
  loading: boolean;
  editing: boolean;
  clientId: string;
  clientSecret: string;
  signingSecret: string;
  error?: string;
};

/** What a status response says about the panel's own state, and nothing else. */
function fromSlackStatus(status: SlackStatus) {
  return {
    configured: status.configured,
    connected: status.connected,
    events: status.events ?? false,
    hasSigningSecret: status.signingSecret ?? false,
  };
}

type ConnectionState =
  | { phase: "unknown" }
  | { phase: "connected" }
  | { phase: "disconnected" }
  | { phase: "connecting"; flow: GitHubConnectResponse }
  | { phase: "error"; message: string };

type ClientIdState =
  | { phase: "loading" }
  | {
      phase: "configured";
      hint: string;
      source: "environment" | "configuration" | "keychain";
    }
  | { phase: "unconfigured" }
  | { phase: "editing"; value: string; saving: boolean };

type SourceControlState =
  | { phase: "loading" }
  | {
      phase: "ready";
      provider: "gh-cli" | "github-oauth" | "gitlab-oauth";
      autoSelected: boolean;
      ghCli: {
        installed: boolean;
        authenticated: boolean;
        account: string;
        message: string;
      } | null;
    };

type GitLabState = {
  origin: string;
  clientId: string;
  configured: boolean;
  connected: boolean;
  loading: boolean;
  editing: boolean;
  saving: boolean;
  flow?: GitLabDeviceFlow;
  error?: string;
};

export function SettingsPanel({ onClose }: { onClose: () => void }) {
  const [connection, setConnection] = useState<ConnectionState>({
    phase: "unknown",
  });
  const [clientId, setClientId] = useState<ClientIdState>({ phase: "loading" });
  const [sourceControl, setSourceControl] = useState<SourceControlState>({
    phase: "loading",
  });
  const [gitLab, setGitLab] = useState<GitLabState>({
    origin: "https://gitlab.com",
    clientId: "",
    configured: false,
    connected: false,
    loading: true,
    editing: false,
    saving: false,
  });
  const [slack, setSlack] = useState<SlackState>({
    configured: false,
    connected: false,
    events: false,
    hasSigningSecret: false,
    loading: true,
    editing: false,
    clientId: "",
    clientSecret: "",
    signingSecret: "",
  });
  // Holds the next-poll timeout ID, not a fixed interval.
  const pollTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Holds the device flow expiry timeout ID.
  const expiryTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const slackPollTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const gitLabPollTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const gitLabExpiryTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollTimeoutRef.current !== null) {
      clearTimeout(pollTimeoutRef.current);
      pollTimeoutRef.current = null;
    }
    if (expiryTimeoutRef.current !== null) {
      clearTimeout(expiryTimeoutRef.current);
      expiryTimeoutRef.current = null;
    }
    if (slackPollTimeoutRef.current !== null) {
      clearTimeout(slackPollTimeoutRef.current);
      slackPollTimeoutRef.current = null;
    }
    if (gitLabPollTimeoutRef.current !== null) {
      clearTimeout(gitLabPollTimeoutRef.current);
      gitLabPollTimeoutRef.current = null;
    }
    if (gitLabExpiryTimeoutRef.current !== null) {
      clearTimeout(gitLabExpiryTimeoutRef.current);
      gitLabExpiryTimeoutRef.current = null;
    }
  }, []);

  const loadClientId = useCallback(() => {
    getGitHubClientId()
      .then((info) => {
        if (info.source === "none") {
          setClientId({ phase: "unconfigured" });
        } else {
          setClientId({
            phase: "configured",
            hint: info.hint,
            source: info.source,
          });
        }
      })
      .catch(() => {
        setClientId({ phase: "unconfigured" });
      });
  }, []);

  // Load status on mount; cancel any timers on unmount.
  useEffect(() => {
    getGitHubStatus()
      .then(({ connected }) => {
        setConnection({ phase: connected ? "connected" : "disconnected" });
      })
      .catch(() => {
        setConnection({ phase: "disconnected" });
      });
    loadClientId();
    getSourceControlProvider().then((status) => {
      setSourceControl({
        phase: "ready",
        provider: status.provider,
        autoSelected: status.autoSelected,
        ghCli: null,
      });
      // OAuth is already selected: its UI does not need a GH CLI subprocess.
      // Load that diagnostic only when it is the active provider.
      if (status.provider === "gh-cli") {
        void getSourceControlStatus().then((cliStatus) => {
          setSourceControl({
            phase: "ready",
            provider: cliStatus.provider,
            autoSelected: cliStatus.autoSelected,
            ghCli: cliStatus.ghCli,
          });
        });
      }
    });
    getSlackStatus()
      .then((status) => setSlack((value) => ({ ...value, ...fromSlackStatus(status), loading: false })))
      .catch(() => setSlack((value) => ({ ...value, loading: false })));
    getGitLabStatus()
      .then((status) =>
        setGitLab((value) => ({
          ...value,
          origin: status.origin,
          configured: status.clientIdConfigured,
          connected: status.connected,
          loading: false,
        })),
      )
      .catch(() => setGitLab((value) => ({ ...value, loading: false })));
    return stopPolling;
  }, [stopPolling, loadClientId]);

  const chooseProvider = useCallback(
    async (provider: "gh-cli" | "github-oauth" | "gitlab-oauth") => {
      if (provider === "github-oauth" || provider === "gitlab-oauth") {
        await setSourceControlProvider(
          provider,
          provider === "gitlab-oauth" ? gitLab.origin : undefined,
        );
        setSourceControl({
          phase: "ready",
          provider,
          autoSelected: false,
          ghCli: sourceControl.phase === "ready" ? sourceControl.ghCli : null,
        });
        return;
      }
      await setSourceControlProvider(provider);
      const status = await getSourceControlStatus();
      setSourceControl({
        phase: "ready",
        provider: status.provider,
        autoSelected: false,
        ghCli: status.ghCli,
      });
    },
    [gitLab.origin, sourceControl],
  );

  const handleSaveClientId = useCallback(async () => {
    if (clientId.phase !== "editing") return;
    const value = clientId.value.trim();
    if (!value) return;
    setClientId({ phase: "editing", value, saving: true });
    try {
      await setGitHubClientId(value);
      await loadClientId();
    } catch {
      // Stay in editing so user can retry
      setClientId({ phase: "editing", value, saving: false });
    }
  }, [clientId, loadClientId]);

  // Schedule one poll tick, using the server-supplied next interval.
  const schedulePoll = useCallback(
    (intervalSeconds: number, flow: GitHubConnectResponse) => {
      pollTimeoutRef.current = setTimeout(async () => {
        pollTimeoutRef.current = null;
        try {
          const result = await pollGitHubConnect();
          if (result.status === "complete") {
            stopPolling();
            setConnection({ phase: "connected" });
          } else {
            // Reschedule using the server-supplied interval, which grows when
            // GitHub returns slow_down.
            schedulePoll(result.nextInterval, flow);
          }
        } catch (err) {
          stopPolling();
          setConnection({
            phase: "error",
            message: err instanceof Error ? err.message : "Connection failed.",
          });
        }
      }, intervalSeconds * 1000);
    },
    [stopPolling],
  );

  const handleConnect = useCallback(async () => {
    setConnection({ phase: "unknown" });
    try {
      const flow = await connectGitHub();
      setConnection({ phase: "connecting", flow });
      schedulePoll(flow.interval, flow);
      // Cancel polling once the device code expires.
      expiryTimeoutRef.current = setTimeout(() => {
        stopPolling();
        setConnection({
          phase: "error",
          message:
            "The authorisation code expired. Click Try again to start over.",
        });
      }, flow.expiresIn * 1000);
    } catch (err) {
      setConnection({
        phase: "error",
        message:
          err instanceof Error ? err.message : "Could not start connection.",
      });
    }
  }, [schedulePoll, stopPolling]);

  const handleDisconnect = useCallback(async () => {
    stopPolling();
    try {
      await disconnectGitHub();
      setConnection({ phase: "disconnected" });
    } catch (err) {
      setConnection({
        phase: "error",
        message: err instanceof Error ? err.message : "Disconnect failed.",
      });
    }
  }, [stopPolling]);

  const handleCancelConnect = useCallback(() => {
    stopPolling();
    setConnection({ phase: "disconnected" });
  }, [stopPolling]);

  const clientIdReady = clientId.phase === "configured";

  const saveGitLabClientId = useCallback(async () => {
    const origin = gitLab.origin.trim();
    const clientId = gitLab.clientId.trim();
    if (!origin || !clientId) return;
    setGitLab((value) => ({ ...value, saving: true, error: undefined }));
    try {
      await setGitLabClientId(origin, clientId);
      const status = await getGitLabStatus(origin);
      setGitLab((value) => ({
        ...value,
        origin: status.origin,
        clientId: "",
        configured: status.clientIdConfigured,
        connected: status.connected,
        editing: false,
        saving: false,
      }));
    } catch (err) {
      setGitLab((value) => ({
        ...value,
        saving: false,
        error: err instanceof Error ? err.message : "Could not save GitLab client ID.",
      }));
    }
  }, [gitLab.clientId, gitLab.origin]);

  const scheduleGitLabPoll = useCallback(
    (origin: string, intervalSeconds: number) => {
      gitLabPollTimeoutRef.current = setTimeout(async () => {
        gitLabPollTimeoutRef.current = null;
        try {
          const result = await pollGitLabConnect(origin);
          if (result.status === "complete") {
            stopPolling();
            setGitLab((value) => ({ ...value, connected: true, flow: undefined }));
          } else {
            scheduleGitLabPoll(origin, result.nextInterval ?? intervalSeconds);
          }
        } catch (err) {
          stopPolling();
          setGitLab((value) => ({
            ...value,
            flow: undefined,
            error: err instanceof Error ? err.message : "GitLab connection failed.",
          }));
        }
      }, intervalSeconds * 1000);
    },
    [stopPolling],
  );

  const startGitLabConnect = useCallback(async () => {
    const origin = gitLab.origin.trim();
    if (!origin) return;
    stopPolling();
    setGitLab((value) => ({ ...value, error: undefined }));
    try {
      const flow = await connectGitLab(origin);
      setGitLab((value) => ({ ...value, origin: flow.origin, flow }));
      scheduleGitLabPoll(flow.origin, flow.interval);
      gitLabExpiryTimeoutRef.current = setTimeout(() => {
        stopPolling();
        setGitLab((value) => ({
          ...value,
          flow: undefined,
          error: "The authorisation code expired. Click Connect GitLab to start over.",
        }));
      }, flow.expiresIn * 1000);
    } catch (err) {
      setGitLab((value) => ({
        ...value,
        error: err instanceof Error ? err.message : "Could not start GitLab connection.",
      }));
    }
  }, [gitLab.origin, scheduleGitLabPoll, stopPolling]);

  const disconnectGitLabAccount = useCallback(async () => {
    stopPolling();
    try {
      await disconnectGitLab(gitLab.origin);
      setGitLab((value) => ({ ...value, connected: false, flow: undefined, error: undefined }));
    } catch (err) {
      setGitLab((value) => ({
        ...value,
        error: err instanceof Error ? err.message : "Could not disconnect GitLab.",
      }));
    }
  }, [gitLab.origin, stopPolling]);

  const saveSlackCredentials = useCallback(async () => {
    const clientId = slack.clientId.trim();
    const clientSecret = slack.clientSecret.trim();
    const signingSecret = slack.signingSecret.trim();
    if (!clientId || !clientSecret) return;
    setSlack((value) => ({ ...value, loading: true, error: undefined }));
    try {
      await setSlackCredentials(clientId, clientSecret, signingSecret || undefined);
      setSlack((value) => ({
        ...value,
        configured: true,
        connected: false,
        events: false,
        loading: false,
        editing: false,
        clientId: "",
        clientSecret: "",
        signingSecret: "",
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Could not save Slack credentials.";
      try {
        const status = await getSlackStatus();
        setSlack((value) => ({ ...value, ...fromSlackStatus(status), loading: false, error: message }));
      } catch {
        setSlack((value) => ({ ...value, loading: false, error: message }));
      }
    }
  }, [slack.clientId, slack.clientSecret, slack.signingSecret]);

  const saveSlackSigningSecret = useCallback(async () => {
    const signingSecret = slack.signingSecret.trim();
    if (!signingSecret) return;
    setSlack((value) => ({ ...value, loading: true, error: undefined }));
    try {
      await setSlackSigningSecret(signingSecret);
      const status = await getSlackStatus();
      setSlack((value) => ({
        ...value,
        ...fromSlackStatus(status),
        loading: false,
        signingSecret: "",
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Could not save the signing secret.";
      setSlack((value) => ({ ...value, loading: false, error: message }));
    }
  }, [slack.signingSecret]);

  const startSlackConnect = useCallback(async () => {
    try {
      const { authorizationUrl } = await connectSlack();
      window.open(authorizationUrl, "slack-oauth", "popup,width=720,height=800");
      setSlack((value) => ({ ...value, loading: true, error: undefined }));
      const deadline = Date.now() + 120_000;
      const poll = async () => {
        try {
          const status = await getSlackStatus();
          if (status.connected || Date.now() >= deadline) {
            setSlack((value) => ({ ...value, ...fromSlackStatus(status), loading: false }));
            return;
          }
          slackPollTimeoutRef.current = setTimeout(() => void poll(), 1000);
        } catch (err) {
          setSlack((value) => ({
            ...value,
            loading: false,
            error: err instanceof Error ? err.message : "Could not check Slack connection.",
          }));
        }
      };
      void poll();
    } catch (err) {
      setSlack((value) => ({
        ...value,
        loading: false,
        error: err instanceof Error ? err.message : "Could not connect Slack.",
      }));
    }
  }, []);

  const removeSlackConnection = useCallback(async () => {
    setSlack((value) => ({ ...value, loading: true, error: undefined }));
    try {
      await disconnectSlack();
      setSlack((value) => ({ ...value, connected: false, loading: false }));
    } catch (err) {
      setSlack((value) => ({
        ...value,
        loading: false,
        error: err instanceof Error ? err.message : "Could not disconnect Slack.",
      }));
    }
  }, []);

  return (
    <div className="settings-panel">
      <div className="settings-panel-head">
        <span className="settings-panel-title">Settings</span>
        <button
          aria-label="Close settings"
          className="settings-panel-close"
          onClick={onClose}
          type="button"
        >
          ×
        </button>
      </div>

      <div className="settings-panel-body">
        <section className="settings-section">
          <h2 className="settings-section-title">GitHub</h2>

          <fieldset className="settings-provider-choice">
            <legend className="settings-label">Source control provider</legend>
            <label>
              <input
                checked={
                  sourceControl.phase === "ready" &&
                  sourceControl.provider === "gh-cli"
                }
                disabled={
                  sourceControl.phase !== "ready" ||
                  (sourceControl.ghCli !== null && !sourceControl.ghCli.installed)
                }
                name="source-control-provider"
                onChange={() => void chooseProvider("gh-cli")}
                type="radio"
              />{" "}
              GH CLI
            </label>
            <label>
              <input
                checked={
                  sourceControl.phase === "ready" &&
                  sourceControl.provider === "github-oauth"
                }
                disabled={sourceControl.phase !== "ready"}
                name="source-control-provider"
                onChange={() => void chooseProvider("github-oauth")}
                type="radio"
              />{" "}
              GitHub OAuth
            </label>
            <label>
              <input
                checked={
                  sourceControl.phase === "ready" &&
                  sourceControl.provider === "gitlab-oauth"
                }
                disabled={sourceControl.phase !== "ready"}
                name="source-control-provider"
                onChange={() => void chooseProvider("gitlab-oauth")}
                type="radio"
              />{" "}
              GitLab OAuth
            </label>
          </fieldset>

          {sourceControl.phase === "loading" && (
            <p className="settings-status settings-status-muted">
              <span aria-hidden="true" className="settings-spinner" />
              Choosing a source control provider…
            </p>
          )}

          {sourceControl.phase === "ready" &&
            sourceControl.provider === "gh-cli" &&
            sourceControl.ghCli === null && (
              <p className="settings-status settings-status-muted">
                <span aria-hidden="true" className="settings-spinner" />
                Checking whether GH CLI is installed and authenticated…
              </p>
            )}

          {sourceControl.phase === "ready" &&
            sourceControl.provider === "gh-cli" &&
            sourceControl.ghCli !== null && (
              <p
                className={
                  sourceControl.ghCli.authenticated
                    ? "settings-status settings-status-ok"
                    : "settings-status settings-status-error"
                }
              >
                {sourceControl.ghCli.message}
                {sourceControl.ghCli.account
                  ? `: ${sourceControl.ghCli.account}`
                  : ""}
              </p>
            )}

          {sourceControl.phase === "ready" && sourceControl.autoSelected && (
            <p className="settings-status settings-status-muted">
              Auto-selected from your authenticated GH CLI session.
            </p>
          )}

          {sourceControl.phase === "ready" &&
            sourceControl.ghCli !== null &&
            !sourceControl.ghCli.installed && (
              <p className="settings-status settings-status-muted">
                Install it with <code>brew install gh</code>, then run{" "}
                <code>gh auth login</code>.
              </p>
            )}

          {sourceControl.phase === "ready" &&
            sourceControl.provider === "github-oauth" && (
              <>
                {/* ── Client ID ───────────────────────────────────────── */}
                {clientId.phase === "loading" && (
                  <p className="settings-status settings-status-muted">
                    Loading…
                  </p>
                )}

                {clientId.phase === "configured" && (
                  <div className="settings-client-id">
                    <span className="settings-client-id-label">
                      Client ID
                      {clientId.source === "environment" && (
                        <span className="settings-client-id-source">
                          {" "}
                          (environment)
                        </span>
                      )}
                      {clientId.source === "configuration" && (
                        <span className="settings-client-id-source">
                          {" "}
                          (configuration)
                        </span>
                      )}
                    </span>
                    <span className="settings-client-id-hint">
                      {clientId.hint}
                    </span>
                    {clientId.source === "keychain" && (
                      <button
                        className="settings-link-button"
                        onClick={() =>
                          setClientId({
                            phase: "editing",
                            value: "",
                            saving: false,
                          })
                        }
                        type="button"
                      >
                        Change
                      </button>
                    )}
                  </div>
                )}

                {(clientId.phase === "unconfigured" ||
                  clientId.phase === "editing") && (
                  <div className="settings-client-id-form">
                    <label
                      className="settings-label"
                      htmlFor="github-client-id"
                    >
                      GitHub OAuth Client ID
                    </label>
                    <input
                      autoComplete="off"
                      className="settings-input"
                      id="github-client-id"
                      onChange={(e) =>
                        setClientId({
                          phase: "editing",
                          value: e.target.value,
                          saving: false,
                        })
                      }
                      placeholder="Ov23li…"
                      type="password"
                      value={clientId.phase === "editing" ? clientId.value : ""}
                    />
                    <div className="settings-actions">
                      <button
                        className="settings-button settings-button-primary"
                        disabled={
                          clientId.phase !== "editing" ||
                          !clientId.value.trim() ||
                          clientId.saving
                        }
                        onClick={handleSaveClientId}
                        type="button"
                      >
                        {clientId.phase === "editing" && clientId.saving
                          ? "Saving…"
                          : "Save"}
                      </button>
                      {clientId.phase === "editing" && (
                        <button
                          className="settings-button"
                          onClick={loadClientId}
                          type="button"
                        >
                          Cancel
                        </button>
                      )}
                    </div>
                  </div>
                )}

                {/* ── Connection status (only when a client ID is set) ── */}
                {clientIdReady && (
                  <>
                    {connection.phase === "unknown" && (
                      <p className="settings-status settings-status-muted">
                        Checking…
                      </p>
                    )}

                    {connection.phase === "connected" && (
                      <>
                        <p className="settings-status settings-status-ok">
                          Connected
                        </p>
                        <div className="settings-actions">
                          <button
                            className="settings-button"
                            onClick={handleConnect}
                            type="button"
                          >
                            Reconnect
                          </button>
                          <button
                            className="settings-button settings-button-danger"
                            onClick={handleDisconnect}
                            type="button"
                          >
                            Disconnect
                          </button>
                        </div>
                      </>
                    )}

                    {connection.phase === "disconnected" && (
                      <>
                        <p className="settings-status settings-status-muted">
                          Not connected
                        </p>
                        <div className="settings-actions">
                          <button
                            className="settings-button settings-button-primary"
                            onClick={handleConnect}
                            type="button"
                          >
                            Connect GitHub
                          </button>
                        </div>
                      </>
                    )}

                    {connection.phase === "connecting" && (
                      <>
                        <p className="settings-status settings-status-muted">
                          Waiting for authorisation
                        </p>
                        <div className="settings-device-flow">
                          <p className="settings-device-flow-instruction">
                            Visit{" "}
                            <a
                              className="settings-link"
                              href={connection.flow.verificationUri}
                              rel="noreferrer"
                              target="_blank"
                            >
                              {connection.flow.verificationUri}
                            </a>{" "}
                            and enter this code:
                          </p>
                          <p className="settings-device-code">
                            {connection.flow.userCode}
                          </p>
                        </div>
                        <div className="settings-actions">
                          <button
                            className="settings-button"
                            onClick={handleCancelConnect}
                            type="button"
                          >
                            Cancel
                          </button>
                        </div>
                      </>
                    )}

                    {connection.phase === "error" && (
                      <>
                        <p className="settings-status settings-status-error">
                          {connection.message}
                        </p>
                        <div className="settings-actions">
                          <button
                            className="settings-button settings-button-primary"
                            onClick={handleConnect}
                            type="button"
                          >
                            Try again
                          </button>
                        </div>
                      </>
                    )}
                  </>
                )}
              </>
            )}
        </section>
        {sourceControl.phase === "ready" &&
          sourceControl.provider === "gitlab-oauth" && (
        <section className="settings-section">
          <h2 className="settings-section-title">GitLab OAuth</h2>
          <p className="settings-status settings-status-muted">
            Connect the GitLab account that this source-control provider will use.
          </p>
          {gitLab.loading && (
            <p className="settings-status settings-status-muted">
              <span aria-hidden="true" className="settings-spinner" /> Checking…
            </p>
          )}
          {!gitLab.loading && (
            <>
              <div className="settings-client-id-form">
                <label className="settings-label" htmlFor="gitlab-origin">
                  GitLab instance URL
                </label>
                <input
                  autoComplete="url"
                  className="settings-input"
                  disabled={gitLab.configured && !gitLab.editing}
                  id="gitlab-origin"
                  onChange={(event) =>
                    setGitLab((value) => ({
                      ...value,
                      origin: event.target.value,
                      configured: false,
                      connected: false,
                    }))
                  }
                  placeholder="https://gitlab.com"
                  type="url"
                  value={gitLab.origin}
                />
                {(!gitLab.configured || gitLab.editing) && (
                  <>
                    <label className="settings-label" htmlFor="gitlab-client-id">
                      GitLab OAuth Client ID
                    </label>
                    <input
                      autoComplete="off"
                      className="settings-input"
                      id="gitlab-client-id"
                      onChange={(event) =>
                        setGitLab((value) => ({ ...value, clientId: event.target.value }))
                      }
                      placeholder="Application ID"
                      type="password"
                      value={gitLab.clientId}
                    />
                    <div className="settings-actions">
                      <button
                        className="settings-button settings-button-primary"
                        disabled={!gitLab.origin.trim() || !gitLab.clientId.trim() || gitLab.saving}
                        onClick={() => void saveGitLabClientId()}
                        type="button"
                      >
                        {gitLab.saving ? "Saving…" : "Save GitLab client ID"}
                      </button>
                      {gitLab.configured && (
                        <button
                          className="settings-button"
                          onClick={() =>
                            setGitLab((value) => ({ ...value, editing: false, clientId: "" }))
                          }
                          type="button"
                        >
                          Cancel
                        </button>
                      )}
                    </div>
                  </>
                )}
              </div>

              {gitLab.configured && !gitLab.editing && (
                <>
                  <p
                    className={
                      gitLab.connected
                        ? "settings-status settings-status-ok"
                        : "settings-status settings-status-muted"
                    }
                  >
                    {gitLab.connected ? "Connected" : "GitLab OAuth client ID saved"}
                  </p>
                  {gitLab.flow && (
                    <div className="settings-device-flow">
                      <p className="settings-device-flow-instruction">
                        Visit{" "}
                        <a
                          className="settings-link"
                          href={gitLab.flow.verificationUri}
                          rel="noreferrer"
                          target="_blank"
                        >
                          {gitLab.flow.verificationUri}
                        </a>{" "}
                        and enter this code:
                      </p>
                      <p className="settings-device-code">{gitLab.flow.userCode}</p>
                    </div>
                  )}
                  <div className="settings-actions">
                    <button
                      className="settings-button settings-button-primary"
                      disabled={Boolean(gitLab.flow)}
                      onClick={() => void startGitLabConnect()}
                      type="button"
                    >
                      {gitLab.flow
                        ? "Waiting for authorisation…"
                        : gitLab.connected
                          ? "Reconnect GitLab"
                          : "Connect GitLab"}
                    </button>
                    <button
                      className="settings-button"
                      onClick={() => setGitLab((value) => ({ ...value, editing: true }))}
                      type="button"
                    >
                      Change client ID
                    </button>
                    {gitLab.connected && (
                      <button
                        className="settings-button settings-button-danger"
                        onClick={() => void disconnectGitLabAccount()}
                        type="button"
                      >
                        Disconnect GitLab
                      </button>
                    )}
                  </div>
                </>
              )}
              {gitLab.error && (
                <p className="settings-status settings-status-error">{gitLab.error}</p>
              )}
            </>
          )}
        </section>
          )}
        <section className="settings-section">
          <h2 className="settings-section-title">Slack</h2>
          {slack.loading && (
            <p className="settings-status settings-status-muted">
              <span aria-hidden="true" className="settings-spinner" /> Checking…
            </p>
          )}
          {slack.error && <p className="settings-status settings-status-error">{slack.error}</p>}
          {!slack.loading && (!slack.configured || slack.editing) && (
            <div className="settings-client-id-form">
              <label className="settings-label" htmlFor="slack-client-id">Slack OAuth Client ID</label>
              <input className="settings-input" id="slack-client-id" autoComplete="off"
                value={slack.clientId} onChange={(event) => setSlack((value) => ({ ...value, clientId: event.target.value }))} />
              <label className="settings-label" htmlFor="slack-client-secret">Slack OAuth Client Secret</label>
              <input className="settings-input" id="slack-client-secret" autoComplete="off" type="password"
                value={slack.clientSecret} onChange={(event) => setSlack((value) => ({ ...value, clientSecret: event.target.value }))} />
              <label className="settings-label" htmlFor="slack-signing-secret">Slack Signing Secret (optional)</label>
              <input className="settings-input" id="slack-signing-secret" autoComplete="off" type="password"
                value={slack.signingSecret} onChange={(event) => setSlack((value) => ({ ...value, signingSecret: event.target.value }))} />
              <div className="settings-actions">
                <button className="settings-button settings-button-primary" type="button"
                  disabled={!slack.clientId.trim() || !slack.clientSecret.trim()} onClick={() => void saveSlackCredentials()}>Save credentials</button>
                {slack.editing && <button className="settings-button" type="button" onClick={() => setSlack((value) => ({ ...value, editing: false }))}>Cancel</button>}
              </div>
            </div>
          )}
          {!slack.loading && slack.configured && !slack.editing && (
            <>
              <p className={`settings-status ${slack.connected ? "settings-status-ok" : "settings-status-muted"}`}>
                {slack.connected ? "Connected" : "OAuth credentials saved"}
              </p>
              <div className="settings-actions">
                <button className="settings-button settings-button-primary" type="button" onClick={() => void startSlackConnect()}>
                  {slack.connected ? "Reconnect" : "Connect Slack"}
                </button>
                <button className="settings-button" type="button" onClick={() => setSlack((value) => ({ ...value, editing: true }))}>Change credentials</button>
                {slack.connected && <button className="settings-button settings-button-danger" type="button" onClick={() => void removeSlackConnection()}>Disconnect</button>}
              </div>
              <p className="settings-status settings-status-muted">
                Add <code>{`${window.location.origin}/api/slack/callback`}</code> as a redirect URL in your Slack app.
              </p>
              {!slack.hasSigningSecret && (
                // Saved on its own, without re-entering the OAuth pair: doing
                // that revokes the token and starts the flow over, which is
                // not a price for turning mentions on.
                <div className="settings-client-id-form">
                  <label className="settings-label" htmlFor="slack-signing-secret-only">
                    Slack Signing Secret
                  </label>
                  <input className="settings-input" id="slack-signing-secret-only" autoComplete="off" type="password"
                    value={slack.signingSecret} onChange={(event) => setSlack((value) => ({ ...value, signingSecret: event.target.value }))} />
                  <div className="settings-actions">
                    <button className="settings-button settings-button-primary" type="button"
                      disabled={!slack.signingSecret.trim()} onClick={() => void saveSlackSigningSecret()}>Save signing secret</button>
                  </div>
                </div>
              )}
              <p className="settings-status settings-status-muted">
                {slack.events
                  ? "Ping the bot in Slack to start a work order; it replies in the thread."
                  : !slack.connected
                    ? // Named first: a mention arriving now is ignored rather than
                      // run in silence, and connecting is what changes that.
                      "Mentions are ignored until Slack is connected — a work order started now could not reply."
                    : slack.hasSigningSecret
                      ? "To start work orders by pinging the bot, set work_orders.repository in engine.toml, and subscribe to app_mention events at "
                      : "To start work orders by pinging the bot, save the app's signing secret above, set work_orders.repository in engine.toml, and subscribe to app_mention events at "}
                {!slack.events && slack.connected && (
                  <code>{`${window.location.origin}/api/slack/events`}</code>
                )}
              </p>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
