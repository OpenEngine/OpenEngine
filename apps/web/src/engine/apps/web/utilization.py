"""How much of each runner's subscription this machine has already spent.

Utilization is not something a CLI mentions on the way past. It belongs to the
account the CLI signed in as, and the provider is the only thing that knows it
-- so this reads the credential the runner already stored and asks the provider
the same question the runner's own usage screen asks. Nothing here starts a
turn: finding out what you have spent should not spend more.

Two providers, two shapes of answer, one shape of reading:

    claude  a five-hour window and a weekly one, from the Claude Code OAuth token
    codex   a weekly window, from the ChatGPT token Codex signed in with

Both land in `RunnerUtilization`, so the interface draws one kind of meter
rather than one per vendor. A runner that cannot be read keeps its place in the
list and says why in `error`: "not signed in" is a fact about a runner worth
printing, not a reason to fail the page. Where a command would fix it, `remedy`
carries that command, because a page that can only say "sign in again" has told
the reader the half they already knew.

Finding the credential is most of the work, and Claude's is the awkward one:
there are two stores, they disagree, and the stale one is not the one you would
guess. See `claude_credentials`.

The last reading is cached on disk because a scrape is two network round trips
and the page should have something to draw before either returns. Opening the
page shows what was true last time and replaces it with what is true now.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from platformdirs import user_cache_path

#: Where each provider answers "what has this account spent". Both are the
#: endpoint the vendor's own CLI reads, called with the vendor's own token.
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"

#: The OAuth beta the Claude Code token is issued under; the endpoint refuses
#: the token without it.
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"

#: The macOS keychain entry Claude Code stores its OAuth token in when there is
#: a keychain to store it in.
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"

#: A week, in seconds, which is how Codex measures the window it meters a
#: subscription over. Its response carries the window length rather than a name,
#: so this is what identifies the weekly one among however many it reports.
WEEK_SECONDS = 7 * 24 * 60 * 60

_HTTP_TIMEOUT_SECONDS = 20.0

#: How long to wait on the keychain before giving up on it. Short: reading an
#: entry this process is already trusted with returns at once, and the only way
#: to spend longer is a dialog waiting on somebody.
_KEYCHAIN_TIMEOUT_SECONDS = 5.0

#: Who is asking. Not decoration: the edge in front of one of these endpoints
#: refuses a request whose agent names a generic HTTP client, so a client that
#: does not say who it is gets a 403 rather than an answer.
_USER_AGENT = "openengine"

#: What to run to sign a runner in again. Carried alongside the error rather
#: than left to the reader to know, because "sign in again" is only useful next
#: to the thing that signs you in -- and neither of these is this application's
#: own login, so nothing here can offer to do it for them.
CLAUDE_SIGN_IN = "claude setup-token"
CODEX_SIGN_IN = "codex login"


class UtilizationError(RuntimeError):
    """A runner's utilization could not be read.

    `remedy` is the command that would fix it, where one exists. A provider
    being briefly unreachable has none; a credential too old to use does.
    """

    def __init__(self, message: str, remedy: str = "") -> None:
        super().__init__(message)
        self.remedy = remedy


@dataclass(frozen=True, slots=True)
class UtilizationWindow:
    """One limit a provider meters a subscription against, and its state.

    `used_percent` is what the provider reported, not a figure derived here.
    `resets_at` is an ISO-8601 instant, or empty where the provider named none
    -- a window nothing has been spent in has no reset to count down to.
    """

    window_id: str
    label: str
    used_percent: float
    resets_at: str = ""


@dataclass(frozen=True, slots=True)
class RunnerUtilization:
    """One runner's reading, whether or not it could be taken.

    A reading that failed carries `error` and no windows. A reading restored
    from the cache carries the `read_at` of the scrape that took it, which is
    what lets the page say how old the figures on it are.
    """

    runner: str
    plan: str = ""
    windows: tuple[UtilizationWindow, ...] = ()
    error: str = ""
    remedy: str = ""
    read_at: float = 0.0


def _iso(epoch_seconds: object) -> str:
    """An instant as the interface prints it, from whatever the provider sent."""
    if not isinstance(epoch_seconds, (int, float)) or isinstance(epoch_seconds, bool):
        return ""
    return datetime.fromtimestamp(float(epoch_seconds), UTC).isoformat()


def _number(value: object) -> float:
    """A figure, or zero where whatever was sent was not one."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


@dataclass(frozen=True, slots=True)
class StoredToken:
    """One Claude Code credential, and when it stops being one.

    `expires_at` is epoch seconds, or zero where the store named none -- a
    token whose lifetime is not written down is taken at face value, because
    the alternative is refusing to try one that may well work.
    """

    access_token: str
    expires_at: float = 0.0

    @property
    def expired(self) -> bool:
        return bool(self.expires_at) and self.expires_at <= time.time()


def _stored_token(account: object) -> StoredToken | None:
    """A credential out of the `claudeAiOauth` object both stores hold."""
    if not isinstance(account, dict):
        return None
    token = account.get("accessToken")
    if not isinstance(token, str) or not token:
        return None
    # Both stores write milliseconds, which is what JavaScript's Date gives.
    return StoredToken(token, _number(account.get("expiresAt")) / 1000)


def _oauth_token_in(path: Path) -> StoredToken | None:
    """The Claude Code OAuth token in a credentials file, if it holds one."""
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _stored_token(stored.get("claudeAiOauth") if isinstance(stored, dict) else None)


def _keychain_token() -> StoredToken | None:
    """The same token, from the macOS keychain Claude Code prefers to a file.

    Read with `security` rather than through a keyring library on purpose: the
    entry belongs to Claude Code, and the CLI is what the OS grants access to.
    Anywhere else this is simply nothing, which the caller reports as "not
    signed in" -- the honest answer when nothing readable holds a token.

    Bounded because this is the one call here that can stop and wait on a
    person: an entry this process is not yet trusted with puts a modal in front
    of whoever is at the machine, and nobody is, on a server. Waiting forever
    would hold the refresh open and the thread it runs on with it, so a prompt
    nobody answers reads the same as no keychain at all.
    """
    if sys.platform != "darwin":
        return None
    try:
        found = subprocess.run(
            ["security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            check=False,
            timeout=_KEYCHAIN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if found.returncode:
        return None
    try:
        stored = json.loads(found.stdout.decode(errors="replace"))
    except json.JSONDecodeError:
        return None
    return _stored_token(stored.get("claudeAiOauth") if isinstance(stored, dict) else None)


def claude_credentials(home: Path | None = None) -> tuple[StoredToken, ...]:
    """Every Claude Code credential this machine holds, newest first.

    There is more than one place to look and they disagree. Claude Code keeps
    the live credential in the keychain and refreshes it in place; the file is
    what platforms without a keychain use, and on a Mac it is usually a
    leftover from before the keychain existed -- months stale, and refused the
    moment it is sent. Preferring one store over the other picks the wrong one
    about half the time, so this collects both and sorts by expiry: the token
    that lasts longest is the one that was refreshed most recently.

    A token given in the environment wins outright. It is the only one somebody
    chose deliberately, and `claude setup-token` issues it to be long-lived,
    so there is nothing to compare its lifetime against.
    """
    from_environment = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if from_environment:
        return (StoredToken(from_environment),)
    found = (
        _keychain_token(),
        _oauth_token_in((home or Path.home()) / ".claude" / ".credentials.json"),
    )
    return tuple(
        sorted(
            (token for token in found if token is not None),
            key=lambda token: token.expires_at,
            reverse=True,
        )
    )


def claude_access_token(home: Path | None = None) -> str:
    """The freshest Claude Code credential still worth sending, if any."""
    for token in claude_credentials(home):
        if not token.expired:
            return token.access_token
    return ""


def codex_credentials(home: Path | None = None) -> tuple[str, str]:
    """Codex's ChatGPT token and the account it belongs to, as a pair.

    Both are needed: the backend reads the account from a header rather than
    from the token, so a token without one is not a usable credential.
    """
    root = Path(os.environ.get("CODEX_HOME") or (home or Path.home()) / ".codex")
    try:
        stored = json.loads((root / "auth.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    tokens = stored.get("tokens") if isinstance(stored, dict) else None
    if not isinstance(tokens, dict):
        return "", ""
    access = tokens.get("access_token")
    account = tokens.get("account_id")
    return (
        access if isinstance(access, str) else "",
        account if isinstance(account, str) else "",
    )


async def _read_json(
    client: httpx.AsyncClient, url: str, headers: Mapping[str, str], *, sign_in: str
) -> dict[str, object]:
    try:
        response = await client.get(
            url,
            headers={"User-Agent": _USER_AGENT, **headers},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as error:
        raise UtilizationError(f"could not reach the provider: {error}") from error
    if response.status_code in (401, 403):
        raise UtilizationError("the stored credential was refused", sign_in)
    if response.status_code == 429:
        # These endpoints are metered in their own right, which is the other
        # half of why the last reading is cached rather than re-taken per view.
        raise UtilizationError("the provider is being asked too often; try again shortly")
    if response.status_code >= 400:
        raise UtilizationError(f"the provider answered {response.status_code}")
    try:
        payload = response.json()
    except ValueError as error:
        raise UtilizationError("the provider did not answer with usage") from error
    if not isinstance(payload, dict):
        raise UtilizationError("the provider did not answer with usage")
    return payload


def _claude_window(payload: Mapping[str, object], key: str, label: str) -> UtilizationWindow | None:
    window = payload.get(key)
    if not isinstance(window, dict):
        return None
    resets_at = window.get("resets_at")
    return UtilizationWindow(
        window_id=key,
        label=label,
        used_percent=_number(window.get("utilization")),
        resets_at=resets_at if isinstance(resets_at, str) else "",
    )


async def read_claude_utilization(
    client: httpx.AsyncClient, home: Path | None = None
) -> RunnerUtilization:
    """Claude's two windows: the five-hour session and the rolling week."""
    stored = await asyncio.to_thread(claude_credentials, home)
    token = next((held.access_token for held in stored if not held.expired), "")
    if not token:
        # Two different things to fix, and the same command fixes both -- but
        # only one of them is worth saying "you are not signed in" about, and
        # it is not the one where the CLI beside this is working fine.
        raise UtilizationError(
            "the stored Claude Code credential has expired"
            if stored
            else "Claude Code is not signed in on this machine",
            CLAUDE_SIGN_IN,
        )
    payload = await _read_json(
        client,
        CLAUDE_USAGE_URL,
        {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": CLAUDE_OAUTH_BETA,
        },
        sign_in=CLAUDE_SIGN_IN,
    )
    windows = [
        window
        for window in (
            _claude_window(payload, "five_hour", "5-hour"),
            _claude_window(payload, "seven_day", "Weekly"),
        )
        if window is not None
    ]
    if not windows:
        raise UtilizationError("the provider reported no usage windows")
    return RunnerUtilization(runner="claude", windows=tuple(windows))


def _codex_weekly(limit: Mapping[str, object]) -> UtilizationWindow | None:
    """The week among however many windows Codex meters this plan over.

    Codex names its windows by position rather than by length, and which
    position holds the week depends on the plan -- so the length is what picks
    it out.
    """
    for key in ("primary_window", "secondary_window"):
        window = limit.get(key)
        if not isinstance(window, dict):
            continue
        if window.get("limit_window_seconds") != WEEK_SECONDS:
            continue
        return UtilizationWindow(
            window_id="weekly",
            label="Weekly",
            used_percent=_number(window.get("used_percent")),
            resets_at=_iso(window.get("reset_at")),
        )
    return None


async def read_codex_utilization(
    client: httpx.AsyncClient, home: Path | None = None
) -> RunnerUtilization:
    """Codex's weekly window, which is the one its subscription is metered on."""
    token, account = await asyncio.to_thread(codex_credentials, home)
    if not token or not account:
        raise UtilizationError("Codex is not signed in on this machine", CODEX_SIGN_IN)
    payload = await _read_json(
        client,
        CODEX_USAGE_URL,
        {"Authorization": f"Bearer {token}", "chatgpt-account-id": account},
        sign_in=CODEX_SIGN_IN,
    )
    limit = payload.get("rate_limit")
    weekly = _codex_weekly(limit) if isinstance(limit, dict) else None
    if weekly is None:
        raise UtilizationError("the provider reported no weekly window")
    plan = payload.get("plan_type")
    return RunnerUtilization(
        runner="codex",
        plan=plan if isinstance(plan, str) else "",
        windows=(weekly,),
    )


#: What each runner name is read with. The keys are the names the interface
#: shows, which is also what `build_runners` binds implementations to -- a
#: runner this deployment does not offer is not scraped, and one nothing here
#: knows how to read is not listed.
UtilizationReader = Callable[[httpx.AsyncClient], Awaitable[RunnerUtilization]]
READERS: Mapping[str, UtilizationReader] = {
    "claude": read_claude_utilization,
    "codex": read_codex_utilization,
}


class UtilizationService:
    """The last reading taken, and the scrape that replaces it.

    The cache is the whole reason the page has something to draw at once. It is
    written after every scrape, including a failed one: a runner that could not
    be read this time keeps the figures it was last read with, so a provider
    being briefly unreachable blanks the message rather than the meters.
    """

    def __init__(
        self,
        cache_path: Path | None = None,
        readers: Mapping[str, UtilizationReader] | None = None,
    ) -> None:
        self._path = cache_path or user_cache_path("openengine") / "utilization.json"
        self._readers = dict(readers if readers is not None else READERS)

    def readable(self, runners: Sequence[str]) -> tuple[str, ...]:
        """Which of this deployment's runners have a way of being read."""
        return tuple(runner for runner in runners if runner in self._readers)

    def cached(self) -> tuple[RunnerUtilization, ...]:
        try:
            stored = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ()
        entries = stored.get("runners") if isinstance(stored, dict) else None
        if not isinstance(entries, list):
            return ()
        return tuple(
            reading
            for reading in (_reading_from_json(entry) for entry in entries)
            if reading is not None
        )

    async def refresh(self, runners: Sequence[str]) -> tuple[RunnerUtilization, ...]:
        """Scrape every readable runner at once and remember what came back."""
        names = self.readable(runners)
        async with httpx.AsyncClient() as client:
            taken = await asyncio.gather(
                *(self._read(name, client) for name in names),
            )
        merged = _merge(self.cached(), taken)
        self._write(merged)
        return merged

    async def _read(self, runner: str, client: httpx.AsyncClient) -> RunnerUtilization:
        try:
            reading = await self._readers[runner](client)
        except UtilizationError as error:
            return RunnerUtilization(runner=runner, error=str(error), remedy=error.remedy)
        except Exception as error:  # noqa: BLE001 -- one runner must not fail the page
            return RunnerUtilization(runner=runner, error=str(error) or type(error).__name__)
        return replace(reading, runner=runner, read_at=time.time())

    def _write(self, readings: Sequence[RunnerUtilization]) -> None:
        """Replace the cache atomically, from a scratch file nothing shares.

        Two open tabs are two refreshes, and a scratch name they both hold would
        let one rename the file the other is still filling -- leaving whichever
        lost as the cache, half written or empty. A name per write means each
        renames only its own, and the last one to finish wins whole.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f"{self._path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps({"runners": [_reading_json(reading) for reading in readings]}) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self._path)
        finally:
            # Only reached when the rename did not happen; a scratch file left
            # behind would never be read and never be cleaned up.
            temporary.unlink(missing_ok=True)


def _merge(
    previous: Sequence[RunnerUtilization], taken: Sequence[RunnerUtilization]
) -> tuple[RunnerUtilization, ...]:
    """Keep the last figures for a runner this scrape could not read.

    The error is still carried, so the page says both what is on screen and why
    it is not newer. A reading that succeeded replaces its predecessor whole.
    """
    kept = {reading.runner: reading for reading in previous}
    merged: list[RunnerUtilization] = []
    for reading in taken:
        stale = kept.get(reading.runner)
        if reading.error and stale is not None and stale.windows:
            merged.append(replace(stale, error=reading.error, remedy=reading.remedy))
        else:
            merged.append(reading)
    return tuple(merged)


def _reading_json(reading: RunnerUtilization) -> dict[str, object]:
    return {
        "runner": reading.runner,
        "plan": reading.plan,
        "error": reading.error,
        "remedy": reading.remedy,
        "readAt": reading.read_at,
        "windows": [
            {
                "windowId": window.window_id,
                "label": window.label,
                "usedPercent": window.used_percent,
                "resetsAt": window.resets_at,
            }
            for window in reading.windows
        ],
    }


def _reading_from_json(entry: object) -> RunnerUtilization | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("runner"), str):
        return None
    windows = entry.get("windows")
    return RunnerUtilization(
        runner=str(entry["runner"]),
        plan=str(entry.get("plan") or ""),
        error=str(entry.get("error") or ""),
        remedy=str(entry.get("remedy") or ""),
        read_at=_number(entry.get("readAt")),
        windows=tuple(
            UtilizationWindow(
                window_id=str(window.get("windowId") or ""),
                label=str(window.get("label") or ""),
                used_percent=_number(window.get("usedPercent")),
                resets_at=str(window.get("resetsAt") or ""),
            )
            for window in (windows if isinstance(windows, list) else ())
            if isinstance(window, dict)
        ),
    )


def utilization_json(readings: Sequence[RunnerUtilization]) -> dict[str, object]:
    """The wire shape the page reads, from either the cache or a scrape."""
    return {"runners": [_reading_json(reading) for reading in readings]}


__all__ = [
    "CLAUDE_SIGN_IN",
    "CLAUDE_USAGE_URL",
    "CODEX_SIGN_IN",
    "CODEX_USAGE_URL",
    "READERS",
    "RunnerUtilization",
    "StoredToken",
    "UtilizationError",
    "UtilizationService",
    "UtilizationWindow",
    "claude_access_token",
    "claude_credentials",
    "codex_credentials",
    "read_claude_utilization",
    "read_codex_utilization",
    "utilization_json",
]
