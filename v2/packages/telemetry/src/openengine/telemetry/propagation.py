"""Trace context across NATS: W3C `traceparent` and `tracestate` message headers.

The contracts allow both headers on every command, query and event
(`common/v1/types.schema.json#/$defs/trace_headers`). A sender wraps the
publish in `sending`, which starts a producer span and returns the headers to
publish with; a receiver wraps its handling in `receiving`, which continues
the sender's trace, or starts one when the message carries none:

    with sending(subject, {"Nats-Msg-Id": event["event_id"]}) as headers:
        await js.publish(subject, body, headers=headers)

    with receiving(message.subject, message.headers):
        await handle(message)

Name spans after the channel's address template (`oe.v1.cxn.control.connections.{connection_id}.disable`)
rather than the subject, whose IDs would give every message its own span name.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Span, SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_PROPAGATOR = TraceContextTextMapPropagator()
_TRACER = "openengine.telemetry"


def inject(headers: Mapping[str, str] | None = None, context: Context | None = None) -> dict[str, str]:
    """`headers` plus the trace context of `context` (the current one by default)."""
    carrier = dict(headers or {})
    _PROPAGATOR.inject(carrier, context=context)
    return carrier


def extract(headers: Mapping[str, str] | None) -> Context:
    """The trace context a message's headers carry; empty when they carry none."""
    return _PROPAGATOR.extract(dict(headers or {}))


def _attributes(subject: str, operation: str) -> dict[str, str]:
    return {"messaging.system": "nats", "messaging.destination.name": subject, "messaging.operation.type": operation}


@contextmanager
def sending(subject: str, headers: Mapping[str, str] | None = None, *, name: str | None = None) -> Iterator[dict[str, str]]:
    """A producer span around publishing to `subject`; yields `headers` with its trace context."""
    tracer = trace.get_tracer(_TRACER)
    with tracer.start_as_current_span(
        f"send {name or subject}", kind=SpanKind.PRODUCER, attributes=_attributes(subject, "send")
    ):
        yield inject(headers)


@contextmanager
def receiving(subject: str, headers: Mapping[str, str] | None, *, name: str | None = None) -> Iterator[Span]:
    """A consumer span around handling a message from `subject`, in the trace its headers carry."""
    tracer = trace.get_tracer(_TRACER)
    with tracer.start_as_current_span(
        f"process {name or subject}",
        context=extract(headers),
        kind=SpanKind.CONSUMER,
        attributes=_attributes(subject, "process"),
    ) as span:
        yield span
