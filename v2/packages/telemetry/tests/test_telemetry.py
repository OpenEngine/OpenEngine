from __future__ import annotations

import io
import json
import logging
import os
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from opentelemetry import trace
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from opentelemetry.util._once import Once

from openengine.telemetry import (
    LogFormat,
    Telemetry,
    TelemetryConfig,
    configure,
    extract,
    inject,
    receiving,
    render,
    sending,
)
from openengine.telemetry.config import ConfigError

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
LOCAL_ONLY = TelemetryConfig(log_format=LogFormat.JSON)


@pytest.fixture(autouse=True)
def fresh_globals() -> Iterator[None]:
    """Let each test's `configure` install its own global tracer provider."""
    trace._TRACER_PROVIDER_SET_ONCE = Once()
    trace._TRACER_PROVIDER = None
    yield
    root = logging.getLogger()
    for handler in [handler for handler in root.handlers if getattr(handler, "_openengine", False)]:
        root.removeHandler(handler)


def lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_reads_otlp_export_from_the_standard_variables() -> None:
    assert TelemetryConfig.from_environment({}) == TelemetryConfig()
    both = TelemetryConfig.from_environment({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318"})
    assert both.export_logs and both.export_traces
    logs_only = TelemetryConfig.from_environment({"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "https://logs.example/v1/logs"})
    assert logs_only.export_logs and not logs_only.export_traces
    no_traces = TelemetryConfig.from_environment(
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318", "OTEL_TRACES_EXPORTER": "none"}
    )
    assert no_traces.export_logs and not no_traces.export_traces
    disabled = TelemetryConfig.from_environment(
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318", "OTEL_SDK_DISABLED": "true"}
    )
    assert not disabled.export_logs and not disabled.export_traces


def test_reads_local_format_and_level() -> None:
    config = TelemetryConfig.from_environment({"OE_LOG_FORMAT": "Console", "OE_LOG_LEVEL": "debug"})
    assert config.log_format is LogFormat.CONSOLE
    assert config.log_level == logging.DEBUG
    with pytest.raises(ConfigError, match="OE_LOG_FORMAT='xml'"):
        TelemetryConfig.from_environment({"OE_LOG_FORMAT": "xml"})
    with pytest.raises(ConfigError, match="OE_LOG_LEVEL='loud'"):
        TelemetryConfig.from_environment({"OE_LOG_LEVEL": "loud"})


def test_json_lines_carry_the_open_telemetry_fields_and_the_current_trace() -> None:
    stream = io.StringIO()
    telemetry = configure("ge", config=LOCAL_ONLY, stream=stream)
    log = logging.getLogger("graph_engine.jobs")

    log.info("outside a span")
    with telemetry.tracer("test").start_as_current_span("work") as span:
        log.warning("job %s started", "job_88", extra={"job_id": "job_88"})

    outside, inside = lines(stream)
    assert "trace_id" not in outside
    assert inside["body"] == "job job_88 started"
    assert inside["severity_text"] == "WARN"
    assert inside["severity_number"] == 13
    assert inside["service.name"] == "openengine-ge"
    assert inside["process.pid"] == os.getpid()
    assert inside["scope"] == "graph_engine.jobs"
    assert inside["timestamp"].endswith("Z")
    assert inside["trace_id"] == trace.format_trace_id(span.get_span_context().trace_id)
    assert inside["span_id"] == trace.format_span_id(span.get_span_context().span_id)
    assert inside["attributes"]["job_id"] == "job_88"
    assert inside["attributes"]["code.function.name"] == "test_json_lines_carry_the_open_telemetry_fields_and_the_current_trace"


class BadGraph(ValueError):
    pass


def apply() -> None:
    raise BadGraph


def test_exceptions_are_semantic_convention_attributes() -> None:
    stream = io.StringIO()
    configure("ge", config=LOCAL_ONLY, stream=stream)
    try:
        apply()
    except BadGraph:
        logging.getLogger("graph_engine").exception("apply failed")

    (line,) = lines(stream)
    assert line["severity_text"] == "ERROR"
    assert line["attributes"]["exception.type"] == "BadGraph"
    assert line["attributes"]["exception.message"] == ""
    assert "Traceback" in line["attributes"]["exception.stacktrace"]


def test_render_turns_json_lines_into_console_lines_and_passes_other_text_through() -> None:
    stream = io.StringIO()
    configure("cxn", config=LOCAL_ONLY, stream=stream)
    logging.getLogger("connection_bus").info("applied", extra={"connection_id": "conn_acme_gh"})
    text = stream.getvalue()

    rendered = render(text)
    assert rendered.endswith("\n")
    assert re.fullmatch(
        rf"\[{os.getpid()}\] \d{{4}}/\d\d/\d\d \d\d:\d\d:\d\d\.\d{{6}} \[INF\] cxn: applied connection_id=conn_acme_gh\n", rendered
    )
    assert "code." not in rendered
    assert render("[1234] 2026/10/22 nats-server: Server is ready\n") == "[1234] 2026/10/22 nats-server: Server is ready\n"


def test_reconfiguring_replaces_its_own_handlers_only() -> None:
    other = logging.NullHandler()
    logging.getLogger().addHandler(other)
    try:
        first, second = io.StringIO(), io.StringIO()
        configure("ge", config=LOCAL_ONLY, stream=first)
        configure("ge", config=LOCAL_ONLY, stream=second)
        logging.getLogger("x").info("once")
        assert first.getvalue() == ""
        assert len(lines(second)) == 1
        assert other in logging.getLogger().handlers
    finally:
        logging.getLogger().removeHandler(other)


def test_extra_exporters_receive_logs_and_spans_with_the_service_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_SERVICE_NAME", "everyone")
    spans, logs = InMemorySpanExporter(), InMemoryLogRecordExporter()
    telemetry = configure(
        "tools", version="1.2.3", config=LOCAL_ONLY, stream=io.StringIO(), span_exporters=[spans], log_exporters=[logs]
    )

    with trace.get_tracer("test").start_as_current_span("invoke") as span:
        logging.getLogger("tool_service").info("invoked", extra={"attempt": 2, "tags": ("a", "b"), "blob": {"k": 1}})
    telemetry.flush()

    (finished,) = spans.get_finished_spans()
    assert finished.resource.attributes["service.name"] == "openengine-tools"
    assert finished.resource.attributes["service.namespace"] == "openengine"
    assert finished.resource.attributes["service.version"] == "1.2.3"
    (exported,) = logs.get_finished_logs()
    record = exported.log_record
    assert record.body == "invoked"
    assert record.trace_id == span.get_span_context().trace_id
    assert record.attributes["attempt"] == 2
    assert record.attributes["tags"] == ("a", "b")
    assert record.attributes["blob"] == "{'k': 1}"
    assert exported.resource.attributes["service.name"] == "openengine-tools"


def test_trace_context_survives_a_nats_round_trip() -> None:
    spans = InMemorySpanExporter()
    telemetry = configure("cxn", config=LOCAL_ONLY, stream=io.StringIO(), span_exporters=[spans])
    subject = "oe.v1.cxn.events.connections.conn_acme_gh.connection.applied"

    with sending(subject, {"Nats-Msg-Id": "evt_1"}, name="oe.v1.cxn.events.connections.{connection_id}.connection.applied") as headers:
        pass
    assert headers["Nats-Msg-Id"] == "evt_1"
    assert set(headers) == {"Nats-Msg-Id", "traceparent"}
    with receiving(subject, headers) as consumer:
        pass
    telemetry.flush()

    producer, received = spans.get_finished_spans()
    assert producer.kind is SpanKind.PRODUCER and received.kind is SpanKind.CONSUMER
    assert producer.name == "send oe.v1.cxn.events.connections.{connection_id}.connection.applied"
    assert received.name == f"process {subject}"
    assert received.context.trace_id == producer.context.trace_id
    assert received.parent is not None and received.parent.span_id == producer.context.span_id
    assert received.attributes["messaging.system"] == "nats"
    assert consumer.get_span_context().trace_id == producer.context.trace_id


def test_receiving_without_headers_starts_a_new_trace() -> None:
    configure("cxn", config=LOCAL_ONLY, stream=io.StringIO())
    with receiving("oe.v1.cxn.control.connections.list", None) as span:
        assert span.get_span_context().is_valid
        assert span.parent is None  # type: ignore[attr-defined]


def test_inject_and_extract_speak_w3c_trace_context() -> None:
    context = extract({"traceparent": TRACEPARENT, "tracestate": "oe=1"})
    remote = trace.get_current_span(context).get_span_context()
    assert trace.format_trace_id(remote.trace_id) == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert inject(context=context) == {"traceparent": TRACEPARENT, "tracestate": "oe=1"}
    assert inject({"a": "b"}) == {"a": "b"}


class Receiver(BaseHTTPRequestHandler):
    received: dict[str, list[bytes]]

    def do_POST(self) -> None:  # noqa: N802
        self.received.setdefault(self.path, []).append(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()

    def log_message(self, *_: object) -> None:
        pass


@pytest.fixture
def collector() -> Iterator[tuple[str, dict[str, list[bytes]]]]:
    received: dict[str, list[bytes]] = {}
    server = HTTPServer(("127.0.0.1", 0), type("Handler", (Receiver,), {"received": received}))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", received
    server.shutdown()


def test_one_endpoint_receives_logs_and_traces_over_otlp(
    collector: tuple[str, dict[str, list[bytes]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint, received = collector
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer test-token")
    telemetry: Telemetry = configure("vcp", config=TelemetryConfig.from_environment(), stream=io.StringIO())

    with trace.get_tracer("test").start_as_current_span("provision") as span:
        logging.getLogger("virtual_control_plane").info("workspace ready")
    telemetry.shutdown()

    assert set(received) == {"/v1/logs", "/v1/traces"}
    traces = ExportTraceServiceRequest.FromString(received["/v1/traces"][0])
    (exported_span,) = traces.resource_spans[0].scope_spans[0].spans
    assert exported_span.name == "provision"
    logs = ExportLogsServiceRequest.FromString(received["/v1/logs"][0])
    resource = {attribute.key: attribute.value.string_value for attribute in logs.resource_logs[0].resource.attributes}
    assert resource["service.name"] == "openengine-vcp"
    (record,) = logs.resource_logs[0].scope_logs[0].log_records
    assert record.body.string_value == "workspace ready"
    assert record.trace_id == span.get_span_context().trace_id.to_bytes(16, "big")
