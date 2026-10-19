"""Bring JetStream's streams in line with infra/nats/streams.

Each file there is one stream's configuration, in the JSON the JetStream API
takes and `nats stream add --config` reads, named after the stream. Migrating
creates the streams that are missing and updates those whose configuration has
drifted. It never deletes a stream, since a stream holds messages.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

import nats
from nats.aio.client import Client
from nats.errors import Error as ClientError

API = "$JS.API.STREAM"
STREAM_NOT_FOUND = 10059
CONNECT_TIMEOUT_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 5.0


class Outcome(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class StreamResult:
    name: str
    outcome: Outcome


class MigrationError(RuntimeError):
    pass


class InvalidStreamConfig(MigrationError):
    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")


class ConnectFailed(MigrationError):
    def __init__(self, url: str, cause: Exception) -> None:
        super().__init__(f"could not connect to NATS at {url}: {cause or type(cause).__name__}")


class JetStreamUnavailable(MigrationError):
    def __init__(self, url: str, cause: Exception) -> None:
        super().__init__(f"JetStream did not answer at {url}: {cause}")


class JetStreamApiError(MigrationError):
    def __init__(self, operation: str, name: str, error: dict[str, Any]) -> None:
        self.code = error.get("err_code")
        super().__init__(f"could not {operation} stream {name}: {error.get('description', error)}")


def streams_directory() -> Path:
    """The packaged infra/nats/streams."""
    with as_file(files("openengine.cli.migrations") / "nats-streams") as path:
        return path


def load_stream_configs(directory: Path) -> list[dict[str, Any]]:
    configs = []
    for path in sorted(directory.glob("*.json")):
        try:
            config = json.loads(path.read_text())
        except ValueError as error:
            raise InvalidStreamConfig(path, f"not valid JSON: {error}") from error
        if not isinstance(config, dict) or config.get("name") != path.stem:
            raise InvalidStreamConfig(path, f'must be an object whose "name" is "{path.stem}"')
        configs.append(config)
    return configs


def drifted(desired: dict[str, Any], actual: dict[str, Any]) -> bool:
    """Whether the server's config differs in any field the file sets; it fills in the rest with defaults."""
    return any(actual.get(key) != value for key, value in desired.items())


async def _call(client: Client, operation: str, name: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = json.dumps(config).encode() if config is not None else b""
    reply = await client.request(f"{API}.{operation.upper()}.{name}", payload, timeout=REQUEST_TIMEOUT_SECONDS)
    response = json.loads(reply.data)
    if error := response.get("error"):
        raise JetStreamApiError(operation, name, error)
    return response


async def _migrate(client: Client, config: dict[str, Any]) -> StreamResult:
    name = config["name"]
    try:
        info = await _call(client, "info", name)
    except JetStreamApiError as error:
        if error.code != STREAM_NOT_FOUND:
            raise
        await _call(client, "create", name, config)
        return StreamResult(name, Outcome.CREATED)
    if not drifted(config, info["config"]):
        return StreamResult(name, Outcome.UNCHANGED)
    await _call(client, "update", name, config)
    return StreamResult(name, Outcome.UPDATED)


async def migrate_streams(url: str, configs: list[dict[str, Any]]) -> list[StreamResult]:
    errors: list[Exception] = []

    async def record(error: Exception) -> None:
        """Keep nats-py from logging connection errors; the last one is raised instead."""
        errors.append(error)

    try:
        # nats-py retries the first connect forever at max_reconnect_attempts=0,
        # so allow one quick retry and then give up.
        client = await nats.connect(
            url,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            allow_reconnect=False,
            max_reconnect_attempts=1,
            reconnect_time_wait=0,
            error_cb=record,
        )
    except (OSError, ClientError, asyncio.TimeoutError) as error:
        raise ConnectFailed(url, errors[-1] if errors else error) from error
    try:
        return [await _migrate(client, config) for config in configs]
    except ClientError as error:
        # No responders means JetStream is not enabled on the server.
        raise JetStreamUnavailable(url, error) from error
    finally:
        await client.close()
