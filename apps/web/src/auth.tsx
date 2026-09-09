import { useEffect, useMemo, useState, type ReactNode } from "react";

import { getAuthStatus, logout, type AuthStatus } from "./api";
import { routeForPath } from "./routes";

function LoginPage() {
  const params = new URLSearchParams(window.location.search);
  const error = params.get("error");
  const destination = window.location.pathname === "/login"
    ? "/"
    : window.location.pathname + window.location.search + window.location.hash;
  const loginUrl = "/api/auth/github/login" + (destination === "/" ? ""
    : "?" + new URLSearchParams({ return_to: destination }).toString());
  return (
    <main className="login-page">
      <div className="login-card">
        <h1>OpenEngine</h1>
        <p className="lede">Sign in to continue</p>
        {error && (
          <p className="notice">
            {error === "expired"
              ? "Login expired. Please try again."
              : error === "denied"
                ? "GitHub authorization was not completed."
                : "Could not verify your GitHub identity. Please try again."}
          </p>
        )}
        <a href={loginUrl} className="btn btn-primary login-btn">
          <svg viewBox="0 0 16 16" width="20" height="20" fill="currentColor" aria-hidden="true">
            <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
          </svg>
          Sign in with GitHub
        </a>
      </div>
    </main>
  );
}

function UserBadge({ auth }: { auth: AuthStatus }) {
  const [error, setError] = useState(false);
  const [pending, setPending] = useState(false);
  async function signOut() {
    setError(false);
    setPending(true);
    try {
      await logout();
      window.location.replace("/login");
    } catch {
      setError(true);
    } finally {
      setPending(false);
    }
  }
  if (!auth.loginRequired || !auth.user) return null;
  return (
    <div className="user-badge" role="group" aria-label={`Signed in as ${auth.user.login}`}>
      <span className="user-badge-name">{auth.user.login}</span>
      <button
        className="user-badge-logout"
        disabled={pending}
        onClick={() => void signOut()}
      >
        Sign out
      </button>
      {error && <p role="alert">Could not sign out. Please try again.</p>}
    </div>
  );
}

function Redirect({ to }: { to: string }) {
  useEffect(() => { window.location.replace(to); }, [to]);
  return <main className="state">Redirecting…</main>;
}

export function AuthGate({ children }: { children: ReactNode }) {
  const route = useMemo(() => routeForPath(window.location.pathname), []);
  const [auth, setAuth] = useState<AuthStatus | null>(null);

  const [authError, setAuthError] = useState(false);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function checkSession() {
      try {
        const status = await getAuthStatus();
        if (active) {
          setAuth(status);
          setAuthError(false);
        }
      } catch {
        if (active) setAuthError(true);
      } finally {
        if (active) timer = setTimeout(checkSession, 30_000);
      }
    }
    void checkSession();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, []);

  if (authError && auth === null)
    return (
      <main className="state state-fatal">
        Could not reach the server. Please refresh to try again.
      </main>
    );

  if (auth === null)
    return <main className="state">Starting openengine…</main>;

  if (auth.loginRequired && !auth.authenticated)
    return <LoginPage />;

  if (route.kind === "login")
    return <Redirect to="/" />;

  return (
    <>
      <UserBadge auth={auth} />
      {children}
    </>
  );
}

