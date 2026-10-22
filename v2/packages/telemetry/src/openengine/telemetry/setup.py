"""`configure`: point a process's logs and traces where the environment says.

Every process calls it once at start, naming its service. It always writes
log lines to stderr and creates spans, so trace IDs reach the log lines even
with nothing exported; with an OTLP endpoint set, logs and spans are also
batched to it over OTLP/HTTP.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TextIO

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogRecordExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from openengine.telemetry.config import LogFormat, TelemetryConfig
from openengine.telemetry.records import ConsoleFormatter, JsonFormatter, OtlpHandler

NAMESPACE = "openengine"


def service_name(service: str) -> str:
    """The `service.name` a service reports: `openengine-cxn`, `openengine-ge`, …"""
    return f"{NAMESPACE}-{service}"


@dataclass
class Telemetry:
    """One process's configured logging and tracing."""

    service: str
    tracer_provider: TracerProvider
    logger_provider: LoggerProvider
    handlers: list[logging.Handler] = field(default_factory=list)

    def tracer(self, name: str) -> trace.Tracer:
        return self.tracer_provider.get_tracer(name)

    def flush(self) -> None:
        for handler in self.handlers:
            handler.flush()
        self.tracer_provider.force_flush()
        self.logger_provider.force_flush()

    def shutdown(self) -> None:
        """Send what is buffered and detach from `logging`."""
        root = logging.getLogger()
        for handler in self.handlers:
            root.removeHandler(handler)
            handler.close()
        self.tracer_provider.shutdown()
        self.logger_provider.shutdown()


def _formatter(log_format: LogFormat, stream: TextIO, service: str) -> logging.Formatter:
    if log_format is LogFormat.AUTO:
        log_format = LogFormat.CONSOLE if stream.isatty() else LogFormat.JSON
    return ConsoleFormatter(service) if log_format is LogFormat.CONSOLE else JsonFormatter(service)


def configure(
    service: str,
    *,
    version: str | None = None,
    config: TelemetryConfig | None = None,
    stream: TextIO | None = None,
    span_exporters: Sequence[SpanExporter] = (),
    log_exporters: Sequence[LogRecordExporter] = (),
) -> Telemetry:
    """Configure `logging` and tracing for the process running `service` (`cxn`, `ge`, `supervisor`, …).

    `span_exporters` and `log_exporters` are sent to as well as OTLP, for a
    custom backend or for tests. The first call installs the global tracer
    provider, so `opentelemetry.trace.get_tracer` reaches it; later calls
    leave the global one in place.
    """
    config = config or TelemetryConfig.from_environment()
    stream = stream or sys.stderr
    name = service_name(service)
    # Our service name wins over OTEL_SERVICE_NAME, which every process inherits alike.
    attributes: dict[str, str | int] = {
        "service.name": name,
        "service.namespace": NAMESPACE,
        "service.instance.id": str(uuid.uuid4()),
        "process.pid": os.getpid(),
    }
    if version:
        attributes["service.version"] = version
    resource = Resource.create(attributes)

    tracer_provider = TracerProvider(resource=resource)
    for exporter in [*([OTLPSpanExporter()] if config.export_traces else []), *span_exporters]:
        tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
    if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
        trace.set_tracer_provider(tracer_provider)

    logger_provider = LoggerProvider(resource=resource)
    log_exporters = [*([OTLPLogExporter()] if config.export_logs else []), *log_exporters]
    for exporter in log_exporters:
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(exporter))

    local = logging.StreamHandler(stream)
    local.setFormatter(_formatter(config.log_format, stream, name))
    handlers: list[logging.Handler] = [local]
    if log_exporters:
        handlers.append(OtlpHandler(logger_provider))

    root = logging.getLogger()
    for handler in [handler for handler in root.handlers if getattr(handler, "_openengine", False)]:
        root.removeHandler(handler)
    for handler in handlers:
        handler._openengine = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(config.log_level)
    return Telemetry(name, tracer_provider, logger_provider, handlers)
