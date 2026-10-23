"""What `configure` reads from the environment.

OTLP export uses OpenTelemetry's own variables, so any collector or OTLP
backend works without settings of ours. `OTEL_EXPORTER_OTLP_ENDPOINT` turns
export on for logs and traces at once; `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` or
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` turns on just one signal.
`OTEL_LOGS_EXPORTER=none` or `OTEL_TRACES_EXPORTER=none` turns one off, and
`OTEL_SDK_DISABLED=true` turns both off. The exporters read the rest
(`OTEL_EXPORTER_OTLP_HEADERS`, timeouts, compression) themselves.

Local output is ours: `OE_LOG_FORMAT` is `json` (JSON lines), `console`, or
`auto` (the default: console on a terminal, JSON lines otherwise), and
`OE_LOG_LEVEL` is a `logging` level name, `INFO` by default.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

LOG_FORMAT_VARIABLE = "OE_LOG_FORMAT"
LOG_LEVEL_VARIABLE = "OE_LOG_LEVEL"
ENDPOINT_VARIABLE = "OTEL_EXPORTER_OTLP_ENDPOINT"
LOGS_ENDPOINT_VARIABLE = "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"
TRACES_ENDPOINT_VARIABLE = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
LOGS_EXPORTER_VARIABLE = "OTEL_LOGS_EXPORTER"
TRACES_EXPORTER_VARIABLE = "OTEL_TRACES_EXPORTER"
SDK_DISABLED_VARIABLE = "OTEL_SDK_DISABLED"


class LogFormat(StrEnum):
    AUTO = "auto"
    JSON = "json"
    CONSOLE = "console"


class ConfigError(ValueError):
    pass


class UnknownLogFormat(ConfigError):
    def __init__(self, name: str) -> None:
        choices = ", ".join(log_format.value for log_format in LogFormat)
        super().__init__(f"{LOG_FORMAT_VARIABLE}={name!r} is not one of {choices}")


class UnknownLogLevel(ConfigError):
    def __init__(self, name: str) -> None:
        super().__init__(f"{LOG_LEVEL_VARIABLE}={name!r} is not a logging level")


def _exports(environment: Mapping[str, str], endpoint: str, exporter: str) -> bool:
    if environment.get(SDK_DISABLED_VARIABLE, "").strip().lower() == "true":
        return False
    if environment.get(exporter, "").strip().lower() == "none":
        return False
    return bool(environment.get(ENDPOINT_VARIABLE) or environment.get(endpoint))


@dataclass(frozen=True)
class TelemetryConfig:
    log_format: LogFormat = LogFormat.AUTO
    log_level: int = logging.INFO
    export_logs: bool = False
    export_traces: bool = False

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] = os.environ) -> TelemetryConfig:
        name = environment.get(LOG_FORMAT_VARIABLE) or LogFormat.AUTO
        try:
            log_format = LogFormat(name.lower())
        except ValueError:
            raise UnknownLogFormat(name) from None
        level_name = environment.get(LOG_LEVEL_VARIABLE) or "INFO"
        level = logging.getLevelNamesMapping().get(level_name.upper())
        if level is None:
            raise UnknownLogLevel(level_name)
        return cls(
            log_format=log_format,
            log_level=level,
            export_logs=_exports(environment, LOGS_ENDPOINT_VARIABLE, LOGS_EXPORTER_VARIABLE),
            export_traces=_exports(environment, TRACES_ENDPOINT_VARIABLE, TRACES_EXPORTER_VARIABLE),
        )
