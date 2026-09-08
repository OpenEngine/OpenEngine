"""Reading how much of each runner's subscription has been spent.

Two providers answer in two shapes and the page draws one, so most of what is
worth asserting is the translation: which window is the week, what a missing
credential reads as, and what is still on screen when a provider cannot be
reached.
"""

import asyncio
import json
import os
import subprocess
import time

import httpx
import pytest

from engine.apps.web import utilization as utilization_module
from engine.apps.web.utilization import (
    CLAUDE_SIGN_IN,
    CLAUDE_USAGE_URL,
    CODEX_SIGN_IN,
    CODEX_USAGE_URL,
    RunnerUtilization,
    StoredToken,
    UtilizationError,
    UtilizationService,
    UtilizationWindow,
    claude_access_token,
    claude_credentials,
    codex_credentials,
    read_claude_utilization,
    read_codex_utilization,
)

#: Trimmed to the fields that are read, in the shape the provider sends them.
CLAUDE_USAGE = {
    "five_hour": {"utilization": 12.0, "resets_at": "2026-09-08T20:10:00+00:00"},
    "seven_day": {"utilization": 41.0, "resets_at": "2026-09-10T02:00:00+00:00"},
    "seven_day_opus": None,
}

CODEX_USAGE = {
    "plan_type": "prolite",
    "rate_limit": {
        "primary_window": {
            "used_percent": 7,
            "limit_window_seconds": 604800,
            "reset_at": 1789484793,
        },
        "secondary_window": None,
    },
    # A second metered feature on a five-hour window: not the subscription's
    # week, and not what the page is asked for.
    "additional_rate_limits": [
        {
            "limit_name": "Spark",
            "rate_limit": {
                "primary_window": {"used_percent": 99, "limit_window_seconds": 18000}
            },
        }
    ],
}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _answers(url: str, payload: object, status_code: int = 200):
    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == url
        return httpx.Response(status_code, json=payload)

    return handle


def _claude_file(home, token: str, *, expires_at: float):
    """The credentials file Claude Code writes where there is no keychain.

    `expiresAt` is milliseconds, which is what a JavaScript `Date` gives and
    what both of Claude Code's stores hold.
    """
    root = home / ".claude"
    root.mkdir(parents=True, exist_ok=True)
    (root / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": token,
                    "refreshToken": "refresh",
                    "expiresAt": int(expires_at * 1000),
                }
            }
        ),
        encoding="utf-8",
    )
    return root


def _codex_home(root, token: str = "token", account: str = "account"):
    """A `CODEX_HOME` holding the auth file Codex writes when it signs in."""
    home = root / ".codex"
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": token, "account_id": account}}),
        encoding="utf-8",
    )
    return home


def test_claude_reports_the_five_hour_window_and_the_week(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oat-token")
    seen: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        assert str(request.url) == CLAUDE_USAGE_URL
        return httpx.Response(200, json=CLAUDE_USAGE)

    async def read() -> RunnerUtilization:
        async with _client(handle) as client:
            return await read_claude_utilization(client)

    reading = asyncio.run(read())

    assert seen["authorization"] == "Bearer oat-token"
    assert reading.windows == (
        UtilizationWindow("five_hour", "5-hour", 12.0, "2026-09-08T20:10:00+00:00"),
        UtilizationWindow("seven_day", "Weekly", 41.0, "2026-09-10T02:00:00+00:00"),
    )


def test_codex_reports_the_week_it_is_metered_on(monkeypatch, tmp_path) -> None:
    """The window is picked by its length, not by where it sits.

    Codex names its windows by position, and a plan with a second metered
    feature reports that one too -- so a page asking for the week has to say
    which week it means.
    """
    monkeypatch.setenv("CODEX_HOME", str(_codex_home(tmp_path)))

    async def read() -> RunnerUtilization:
        async with _client(_answers(CODEX_USAGE_URL, CODEX_USAGE)) as client:
            return await read_codex_utilization(client)

    reading = asyncio.run(read())

    assert reading.plan == "prolite"
    assert reading.windows == (
        UtilizationWindow("weekly", "Weekly", 7.0, "2026-09-15T15:06:33+00:00"),
    )


def test_codex_credentials_are_a_pair_or_nothing(monkeypatch, tmp_path) -> None:
    """A token with no account cannot be sent: the backend reads the account
    from a header of its own."""
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "token"}}), encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(home))

    assert codex_credentials() == ("token", "")


def test_a_keychain_prompt_nobody_answers_is_given_up_on(monkeypatch, tmp_path) -> None:
    """The one call here that can stop and wait on a person, bounded.

    Reading an entry this process is not yet trusted with puts a dialog in front
    of whoever is at the machine, and on a server nobody is. Unbounded, that
    would hold the refresh open for as long as the dialog stood, and the thread
    it was dispatched to with it.
    """
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(utilization_module.sys, "platform", "darwin")
    waited: list[object] = []

    def never_answers(argv, **options):
        waited.append(options.get("timeout"))
        raise subprocess.TimeoutExpired(argv, options.get("timeout") or 0)

    monkeypatch.setattr(utilization_module.subprocess, "run", never_answers)

    # No file to read either, so the keychain is the only place left to look.
    assert claude_access_token(home=tmp_path) == ""
    assert len(waited) == 1
    assert isinstance(waited[0], (int, float)) and waited[0] > 0


def test_two_refreshes_at_once_leave_a_whole_cache(monkeypatch, tmp_path) -> None:
    """Two open tabs are two refreshes, and both of them write.

    A scratch file they shared would let one rename the file the other was
    still filling, leaving whichever lost as the cache -- half written, or
    empty. Each writes its own and renames only that.
    """
    path = tmp_path / "utilization.json"
    renamed: list[str] = []
    replace_file = os.replace

    def record(source, destination):
        renamed.append(str(source))
        replace_file(source, destination)

    monkeypatch.setattr(utilization_module.os, "replace", record)

    async def reader(name: str) -> RunnerUtilization:
        # Yield, so the two refreshes are genuinely interleaved rather than run
        # one after the other by an event loop with nothing else to do.
        await asyncio.sleep(0)
        return RunnerUtilization(runner=name, windows=(UtilizationWindow(name, "Weekly", 3.0),))

    service = UtilizationService(
        cache_path=path,
        readers={
            "claude": lambda _client: reader("claude"),
            "codex": lambda _client: reader("codex"),
        },
    )

    async def both():
        return await asyncio.gather(service.refresh(("claude",)), service.refresh(("codex",)))

    asyncio.run(both())

    assert len(renamed) == 2
    assert len(set(renamed)) == 2, "both refreshes wrote through the same scratch file"
    # Whichever finished last is the cache, and it is a whole one.
    stored = service.cached()
    assert len(stored) == 1
    assert stored[0].windows[0].used_percent == 3.0
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["utilization.json"]


def test_a_runner_nobody_has_signed_in_says_so(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty"))

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a runner with no credential must not be asked")

    async def read() -> None:
        async with _client(refuse) as client:
            await read_codex_utilization(client)

    with pytest.raises(UtilizationError, match="not signed in") as refused:
        asyncio.run(read())

    # And it says what would fix it, which is the whole of what the reader can
    # act on -- neither sign-in is one this application could start for them.
    assert refused.value.remedy == CODEX_SIGN_IN


def test_a_refused_credential_is_reported_as_one(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CODEX_HOME", str(_codex_home(tmp_path)))

    async def read() -> None:
        async with _client(_answers(CODEX_USAGE_URL, {}, 401)) as client:
            await read_codex_utilization(client)

    with pytest.raises(UtilizationError, match="refused") as refused:
        asyncio.run(read())

    assert refused.value.remedy == CODEX_SIGN_IN


def test_the_live_store_is_preferred_to_a_stale_one(monkeypatch, tmp_path) -> None:
    """The bug this cost a report: two stores, and the wrong one preferred.

    Claude Code keeps the live credential in the macOS keychain and refreshes
    it in place. `~/.claude/.credentials.json` is what platforms without a
    keychain use, and on a Mac it is usually a leftover from before there was
    one -- months stale, and refused the moment it is sent. Reading the file
    first meant the page reported an expired credential while the CLI beside
    it worked perfectly.
    """
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    now = time.time()
    _claude_file(tmp_path, "stale-token", expires_at=now - 30 * 24 * 60 * 60)
    monkeypatch.setattr(
        utilization_module,
        "_keychain_token",
        lambda: StoredToken("live-token", now + 3600),
    )

    assert [held.access_token for held in claude_credentials(home=tmp_path)] == [
        "live-token",
        "stale-token",
    ]
    assert claude_access_token(home=tmp_path) == "live-token"


def test_a_machine_with_only_a_file_still_reads_it(monkeypatch, tmp_path) -> None:
    """Preferring the keychain is not the same as requiring one.

    Nothing outside macOS has one, and a token there is the only token there
    is.
    """
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    _claude_file(tmp_path, "file-token", expires_at=time.time() + 3600)
    monkeypatch.setattr(utilization_module, "_keychain_token", lambda: None)

    assert claude_access_token(home=tmp_path) == "file-token"


def test_a_token_given_in_the_environment_wins_outright(monkeypatch, tmp_path) -> None:
    """`claude setup-token` issues a long-lived token nothing writes an expiry
    for, so there is nothing to compare it against -- and it is the only one
    somebody chose deliberately."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "chosen-token")
    _claude_file(tmp_path, "file-token", expires_at=time.time() + 3600)

    assert claude_access_token(home=tmp_path) == "chosen-token"


def test_every_stored_credential_expired_is_not_signed_out(monkeypatch, tmp_path) -> None:
    """Two different things, and the page has to tell them apart.

    "You are not signed in" is the wrong thing to print at somebody whose CLI
    is signed in and working; what is true is that what this found is too old
    to send. Neither is worth spending a request on.
    """
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    _claude_file(tmp_path, "stale-token", expires_at=time.time() - 60)
    monkeypatch.setattr(utilization_module, "_keychain_token", lambda: None)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an expired credential must not be sent")

    async def read() -> None:
        async with _client(refuse) as client:
            await read_claude_utilization(client, home=tmp_path)

    with pytest.raises(UtilizationError, match="has expired") as expired:
        asyncio.run(read())

    assert expired.value.remedy == CLAUDE_SIGN_IN


def test_only_the_runners_this_deployment_offers_are_scraped(tmp_path) -> None:
    """The list is the composed runners, narrowed to the ones with a reader.

    A deployment offering a third CLI does not get an empty card for it, and a
    deployment offering only one does not get the other one's.
    """
    asked: list[str] = []

    async def reader(name: str, _client: httpx.AsyncClient) -> RunnerUtilization:
        asked.append(name)
        return RunnerUtilization(runner=name, windows=(UtilizationWindow("w", "Weekly", 3.0),))

    service = UtilizationService(
        cache_path=tmp_path / "utilization.json",
        readers={
            "claude": lambda client: reader("claude", client),
            "codex": lambda client: reader("codex", client),
        },
    )

    readings = asyncio.run(service.refresh(("codex", "gemini")))

    assert asked == ["codex"]
    assert [reading.runner for reading in readings] == ["codex"]
    assert readings[0].read_at > 0


def test_the_cache_answers_before_any_provider_does(tmp_path) -> None:
    """What the page draws on open: the last scrape, without a network call."""
    path = tmp_path / "utilization.json"
    scraped = UtilizationService(
        cache_path=path,
        readers={
            "claude": lambda _client: _reading(
                RunnerUtilization(
                    runner="claude",
                    plan="max",
                    windows=(UtilizationWindow("five_hour", "5-hour", 12.0, "2026-09-08T20:10:00+00:00"),),
                )
            )
        },
    )
    asyncio.run(scraped.refresh(("claude",)))

    reopened = UtilizationService(cache_path=path, readers={})

    cached = reopened.cached()
    assert [reading.runner for reading in cached] == ["claude"]
    assert cached[0].plan == "max"
    assert cached[0].windows[0].used_percent == 12.0
    assert cached[0].read_at > 0


def test_a_failed_scrape_keeps_the_last_figures_and_says_why(tmp_path) -> None:
    """A provider being briefly unreachable blanks the message, not the meters."""
    path = tmp_path / "utilization.json"
    windows = (UtilizationWindow("weekly", "Weekly", 7.0),)
    good = UtilizationService(
        cache_path=path,
        readers={"codex": lambda _client: _reading(RunnerUtilization("codex", windows=windows))},
    )
    asyncio.run(good.refresh(("codex",)))

    async def fail(_client: httpx.AsyncClient) -> RunnerUtilization:
        raise UtilizationError("could not reach the provider")

    after = asyncio.run(
        UtilizationService(cache_path=path, readers={"codex": fail}).refresh(("codex",))
    )

    assert after[0].windows == windows
    assert after[0].error == "could not reach the provider"
    # And that is what the next open draws, error included: the figures are old
    # and the page has to be able to say so.
    assert UtilizationService(cache_path=path, readers={}).cached() == after


def test_no_cache_yet_reads_as_nothing_rather_than_failing(tmp_path) -> None:
    assert UtilizationService(cache_path=tmp_path / "missing.json", readers={}).cached() == ()


async def _reading(value: RunnerUtilization) -> RunnerUtilization:
    return value
