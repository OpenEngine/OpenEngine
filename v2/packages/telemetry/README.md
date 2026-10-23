## Telemetry
`openengine-telemetry`, the logging and tracing layer every process shares.
Each one calls `configure` once at start, naming its service:

```python
from openengine.telemetry import configure

telemetry = configure("cxn")      # service.name = openengine-cxn
...
telemetry.shutdown()              # send what is buffered
```

After that, code logs with the standard library (`logging.getLogger(__name__)`,
fields passed with `extra=`) and traces with the OpenTelemetry API
(`opentelemetry.trace.get_tracer(__name__)`). Nothing else imports this package.

Every process always does two things:

- **JSON lines on stderr**, one object per record with the OpenTelemetry log
  data model's field names (`timestamp`, `severity_text`, `severity_number`,
  `body`, `service.name`, `scope`, `trace_id`, `span_id`, `attributes`).
  Under `oe up` they land in the supervisor's log, which `oe logs` prints in
  nats-server's layout, `[pid] date time [INF] service: body key=value… trace_id=…`,
  so NATS's lines and ours read as one log (`oe logs --json` prints them as written). Any log shipper
  (Vector, Fluent Bit, the Datadog agent, Promtail) can read them as they are.
- **Spans**, so every log line inside one carries its trace ID even when
  nothing is exported.

And with an OTLP endpoint set, it also batches logs and spans to it over
OTLP/HTTP.

| Variable | Default | |
| -- | -- | -- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | | one base URL for every service; logs go to `/v1/logs`, traces to `/v1/traces` |
| `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | | the full URL for one signal, used as is |
| `OTEL_EXPORTER_OTLP_HEADERS` | | `key=value,…`, URL-encoded: authentication for hosted backends |
| `OTEL_LOGS_EXPORTER`, `OTEL_TRACES_EXPORTER` | | `none` turns off one signal |
| `OTEL_SDK_DISABLED` | | `true` turns off OTLP export |
| `OTEL_TRACES_SAMPLER`, `OTEL_RESOURCE_ATTRIBUTES`, … | | the rest of [OpenTelemetry's variables](https://opentelemetry.io/docs/specs/otel/configuration/sdk-environment-variables/) |
| `OE_LOG_FORMAT` | `auto` | `json`, `console`, or `auto`: console on a terminal, JSON lines otherwise |
| `OE_LOG_LEVEL` | `INFO` | any `logging` level name |

`oe up` passes its environment to everything it starts, so one variable sets
the destination for all of them. Resources carry `service.name`
(`openengine-<service>`; it takes precedence over `OTEL_SERVICE_NAME`, which
every process would inherit alike), `service.namespace=openengine` and a
`service.instance.id` per process. Add more, such as `deployment.environment.name`,
with `OTEL_RESOURCE_ATTRIBUTES`.

### Traces across NATS
Commands, queries and events may carry the W3C `traceparent` and `tracestate`
headers (`common/v1/types.schema.json#/$defs/trace_headers` in the
[contracts](../contracts/README.md)). Wrap a publish in `sending` and the
handling of a message in `receiving`, and the receiver's spans and log lines
join the sender's trace:

```python
from openengine.telemetry import receiving, sending

with sending(subject, {"Nats-Msg-Id": event_id}, name=template) as headers:
    await js.publish(subject, body, headers=headers)

with receiving(message.subject, message.headers, name=template):
    await handle(message)
```

Pass the channel's address template as `name`, so span names do not include
IDs. `inject` and `extract` are the same propagation without the spans.

### Sending telemetry somewhere
**Your own stack, locally.** `grafana/otel-lgtm` is an OTLP collector with
Grafana, Loki (logs), Tempo (traces) and Prometheus in one container:

```bash
docker run --rm -p 3000:3000 -p 4318:4318 grafana/otel-lgtm
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 oe up
```

Open Grafana at `http://localhost:3000` (admin / admin); logs are under the Loki
data source and traces under Tempo, and a log line's `trace_id` links to its trace.

**Your own collector.** Run the [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/)
to filter, sample or fan out to several backends, and point the services at it:

```yaml
# collector.yaml
receivers:
  otlp:
    protocols:
      http: { endpoint: 0.0.0.0:4318 }
processors:
  batch: {}
exporters:
  debug: { verbosity: basic }
  # otlphttp: { endpoint: https://your-backend.example }
service:
  pipelines:
    logs: { receivers: [otlp], processors: [batch], exporters: [debug] }
    traces: { receivers: [otlp], processors: [batch], exporters: [debug] }
```

```bash
docker run --rm -p 4318:4318 -v "$PWD/collector.yaml:/etc/otelcol-contrib/config.yaml" otel/opentelemetry-collector-contrib
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 oe up
```

**PostHog.** [PostHog's logs](https://posthog.com/docs/logs/installation/python)
take OTLP with the project token (`phc_…`, not a personal API key) as a bearer
token. Set the signal's own URL; its [tracing](https://posthog.com/docs/distributed-tracing/installation/python)
is in alpha, so send traces there only if your project has it:

```bash
export OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=https://us.i.posthog.com/i/v1/logs   # eu.i.posthog.com in the EU
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20phc_..."
export OTEL_TRACES_EXPORTER=none
oe up
```

**Any other OTLP backend** (Honeycomb, Grafana Cloud, Datadog, New Relic,
Jaeger…) works the same way: its OTLP/HTTP URL in `OTEL_EXPORTER_OTLP_ENDPOINT`
and its key in `OTEL_EXPORTER_OTLP_HEADERS`.

**In process.** `configure(..., span_exporters=[...], log_exporters=[...])`
adds OpenTelemetry SDK exporters alongside OTLP, for a backend without OTLP or
for tests.
