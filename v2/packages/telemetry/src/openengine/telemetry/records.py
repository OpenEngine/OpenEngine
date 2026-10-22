"""`logging` records as OpenTelemetry log records, written as JSON lines or sent over OTLP.

Both outputs share one translation, so a JSON line carries the same fields as
its OTLP record, named after the OpenTelemetry log data model:

    {"timestamp": "2026-10-22T14:03:07.120394Z", "severity_text": "INFO", "severity_number": 9,
     "body": "connection applied", "service.name": "openengine-cxn", "process.pid": 73110, "scope": "connection_bus.handlers",
     "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "span_id": "00f067aa0ba902b7",
     "attributes": {"connection_id": "conn_acme_gh", "code.function.name": "apply", ...}}

`trace_id` and `span_id` are the span current when the record was logged, and
are left out outside a span. Anything passed with `extra=` becomes an attribute.

For a person they are rendered in nats-server's layout, so the two read as one
log: `[pid] date local-time [INF] service: body key=value… trace_id=…`.
"""

from __future__ import annotations

import json
import logging
import traceback
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from opentelemetry._logs import LoggerProvider, LogRecord, SeverityNumber
from opentelemetry.context import get_current

# Python's names for the levels OpenTelemetry spells differently.
_SEVERITY_TEXT = {"WARNING": "WARN", "CRITICAL": "FATAL"}
_SEVERITY_NUMBER = {
    logging.DEBUG: SeverityNumber.DEBUG,
    logging.INFO: SeverityNumber.INFO,
    logging.WARNING: SeverityNumber.WARN,
    logging.ERROR: SeverityNumber.ERROR,
    logging.CRITICAL: SeverityNumber.FATAL,
}
_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}
# nats-server's level tags, by OpenTelemetry severity number range (TRACE 1-4, DEBUG 5-8, …).
_LEVEL_TAGS = ("TRC", "DBG", "INF", "WRN", "ERR", "FTL")
# The exporters' own records; sending them over OTLP could loop when the endpoint is failing.
_EXPORTER_LOGGERS = ("opentelemetry", "urllib3", "requests")


def severity_number(level: int) -> SeverityNumber:
    """The nearest OpenTelemetry severity at or below `level`."""
    known = [threshold for threshold in _SEVERITY_NUMBER if threshold <= level]
    return _SEVERITY_NUMBER[max(known)] if known else SeverityNumber.UNSPECIFIED


def severity_text(record: logging.LogRecord) -> str:
    return _SEVERITY_TEXT.get(record.levelname, record.levelname)


def attributes(record: logging.LogRecord) -> dict[str, Any]:
    """The record's `extra=` fields, where it was logged, and its exception, as semantic-convention attributes."""
    values = {key: value for key, value in vars(record).items() if key not in _RESERVED}
    values["code.file.path"] = record.pathname
    values["code.function.name"] = record.funcName
    values["code.line.number"] = record.lineno
    if record.exc_info and record.exc_info[0] is not None:
        kind, error, _ = record.exc_info
        values["exception.type"] = kind.__name__
        values["exception.message"] = str(error)
        values["exception.stacktrace"] = "".join(traceback.format_exception(*record.exc_info))
    return values


def fields(record: logging.LogRecord, service: str) -> dict[str, Any]:
    """The record as one JSON line's fields."""
    line: dict[str, Any] = {
        "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "severity_text": severity_text(record),
        "severity_number": severity_number(record.levelno).value,
        "body": record.getMessage(),
        "service.name": service,
        "process.pid": record.process,
        "scope": record.name,
    }
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        line["trace_id"] = trace.format_trace_id(context.trace_id)
        line["span_id"] = trace.format_span_id(context.span_id)
    line["attributes"] = attributes(record)
    return line


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(fields(record, self.service), default=str)


def _local_time(timestamp: Any) -> str:
    try:
        when = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return str(timestamp)
    return when.strftime("%Y/%m/%d %H:%M:%S.%f")


def _level_tag(line: dict[str, Any]) -> str:
    number = line.get("severity_number")
    if isinstance(number, int) and 1 <= number <= 24:
        return _LEVEL_TAGS[(number - 1) // 4]
    return str(line.get("severity_text", ""))[:3].upper()


def console(line: dict[str, Any]) -> str:
    """One JSON line's fields in nats-server's layout: pid, local time, level, service, body, attributes, trace."""
    extras = {
        key: value
        for key, value in (line.get("attributes") or {}).items()
        if not key.startswith(("code.", "exception."))
    }
    service = str(line.get("service.name", "")).removeprefix("openengine-")
    parts = [
        f"[{line.get('process.pid', '')}]",
        _local_time(line.get("timestamp", "")),
        f"[{_level_tag(line)}]",
        f"{service}:",
        str(line.get("body", "")),
    ]
    parts += [f"{key}={value}" for key, value in extras.items()]
    if trace_id := line.get("trace_id"):
        parts.append(f"trace_id={trace_id}")
    text = " ".join(part for part in parts if part)
    if stacktrace := (line.get("attributes") or {}).get("exception.stacktrace"):
        text += "\n" + str(stacktrace).rstrip("\n")
    return text


class ConsoleFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        return console(fields(record, self.service))


def render(text: str) -> str:
    """A log line for a person: a JSON line in console form, anything else (such as NATS's own output) unchanged."""
    try:
        line = json.loads(text)
    except ValueError:
        return text
    if not isinstance(line, dict) or "severity_text" not in line or "body" not in line:
        return text
    return console(line) + ("\n" if text.endswith("\n") else "")


class OtlpHandler(logging.Handler):
    """Sends records to a `LoggerProvider`, in the trace context they were logged in."""

    def __init__(self, provider: LoggerProvider, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self.provider = provider

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_EXPORTER_LOGGERS):
            return
        try:
            self.provider.get_logger(record.name).emit(
                LogRecord(
                    timestamp=int(record.created * 1e9),
                    context=get_current(),
                    severity_text=severity_text(record),
                    severity_number=severity_number(record.levelno),
                    body=record.getMessage(),
                    attributes={key: _attribute(value) for key, value in attributes(record).items()},
                )
            )
        except Exception:  # noqa: BLE001 -- as `logging`'s own handlers do, report and carry on
            self.handleError(record)


def _attribute(value: Any) -> Any:
    """OTLP attributes are scalars or lists of scalars; anything else goes as its text."""
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(item, (str, bool, int, float)) for item in value):
        return list(value)
    return str(value)
