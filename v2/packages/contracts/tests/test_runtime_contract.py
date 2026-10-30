from copy import deepcopy
import json

import pytest
from openengine.contracts import runtime
import yaml


def example(name):
    return json.loads((runtime.CONTRACT / "examples" / f"{name}.json").read_text())


def errors(case):
    return runtime.validate_message(
        case["definition"],
        case["payload"],
        case.get("subject", case.get("in_reply_to")),
    )


def test_examples_and_all_message_schemas():
    assert runtime.check() == []
    document = yaml.safe_load((runtime.CONTRACT / "asyncapi.yaml").read_text())
    definitions = json.loads((runtime.CONTRACT / "messages.schema.json").read_text())[
        "$defs"
    ]
    for message in document["components"]["messages"].values():
        definition = message["payload"]["schema"]["$ref"].split("/")[-1]
        assert definition in definitions
    cases = [
        json.loads(path.read_text())
        for path in (runtime.CONTRACT / "examples").glob("*.json")
    ]
    assert set(runtime.DEFINITIONS) - {"update"} <= {
        case["definition"] for case in cases
    }
    for case in cases:
        if case["definition"] in (
            "ready",
            "started",
            "progress",
            "completed",
            "failed",
            "cancelled",
        ):
            assert list(runtime.validator("update").iter_errors(case["payload"])) == []


def test_execute_retry_preserves_complete_identity():
    assert example("execute")["payload"] == example("execute-retry")["payload"]


@pytest.mark.parametrize(
    ("name", "path", "value"),
    [
        ("execute", ("target", "runtime_generation"), 0),
        ("execute", ("target", "attempt_id"), "ws_wrong"),
        ("execute", ("execution", "command_id"), "different"),
        ("execute", ("execution", "cause", "causal_path"), ["wrong"]),
        (
            "execute",
            ("execution", "cause", "causal_path"),
            [f"ancestor_{n}" for n in range(31)] + ["cmd_execute"],
        ),
        ("execute", ("payload", "context", "working_directory"), "../escape"),
        ("execute", ("payload", "context", "instruction_files"), ["/host/secret"]),
        ("execute", ("payload", "actor"), "untrusted"),
        ("cancel", ("command_id",), "rtc_execute"),
        (
            "accepted",
            ("error",),
            {"code": "UNAVAILABLE", "message": "no", "retryable": True},
        ),
        ("stale-runtime", ("error", "retryable"), True),
        ("completed", ("termination",), "never_started"),
        ("completed", ("sequence",), 0),
        ("event-gap", ("error", "retryable"), False),
        ("inspect-response", ("result", "last_event_sequence"), 0),
    ],
)
def test_rejects_invalid_wire_variants(name, path, value):
    case = deepcopy(example(name))
    node = case["payload"]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert errors(case)


@pytest.mark.parametrize(
    ("name", "field"),
    [
        ("cancelled", "termination"),
        ("cancelled", "cause"),
        ("event-gap", "expected_sequence"),
        ("stale-runtime", "error"),
    ],
)
def test_required_safety_fields(name, field):
    case = example(name)
    del case["payload"][field]
    assert errors(case)


def test_subject_and_provisioned_binding_checks():
    case = example("execute")
    case["subject"] = case["subject"].replace("rt_demo", "rt_other")
    assert errors(case)
    payload = example("started")["payload"]
    assert (
        runtime.binding_errors(
            payload,
            {
                "workspace_id": "ws_demo",
                "runtime_id": "rt_demo",
                "runtime_generation": 1,
            },
        )
        == []
    )
    assert runtime.binding_errors(payload, {"runtime_generation": 2})
    assert runtime.binding_errors(payload, {"workspace_id": "ws_other"})


def test_inspect_response_must_bind_snapshot_to_runtime():
    case = example("inspect-response")
    case["payload"]["result"]["attempts"][0]["target"]["runtime_generation"] = 2
    assert errors(case)


def test_progress_usage_and_artifact_bindings():
    case = example("progress")
    case["payload"]["payload"] = {
        "kind": "usage",
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "period_start": "2026-10-22T12:00:00Z",
        "period_end": "2026-10-22T12:00:01Z",
    }
    assert errors(case) == []
    case["payload"]["payload"]["period_end"] = "2026-10-22T11:00:00Z"
    assert errors(case)
    artifact_example = json.loads(
        (
            runtime.ROOT
            / "virtual_control_plane/v1/examples/artifact.created.event.json"
        ).read_text()
    )
    artifact = artifact_example["payload"]["payload"]["artifact"]
    artifact.update(workspace_id="ws_demo", attempt_id="attempt_demo")
    case["payload"]["payload"] = {"kind": "artifact", "artifact": artifact}
    assert errors(case) == []
    artifact["attempt_id"] = "attempt_other"
    assert errors(case)


def test_internal_subjects_are_outside_public_streams():
    document = yaml.safe_load((runtime.CONTRACT / "asyncapi.yaml").read_text())
    streams = runtime.ROOT.parents[1] / "infra/nats/streams"
    for channel in document["channels"].values():
        address = channel["address"]
        if address:
            assert address.startswith("oe.internal.v1.")
            for path in streams.glob("*.json"):
                for subject in json.loads(path.read_text())["subjects"]:
                    assert not address.startswith(subject.removesuffix(">"))


def test_wire_limit_accepts_unicode_without_ascii_reserialization():
    case = example("progress")
    case["payload"]["payload"]["text"] = "😀" * 90_000
    wire = json.dumps(case["payload"], ensure_ascii=False).encode("utf-8")
    assert len(wire) < runtime.MAX_WIRE_BYTES
    assert len(json.dumps(case["payload"]).encode("utf-8")) > runtime.MAX_WIRE_BYTES
    assert runtime.validate_wire_message("progress", wire, case["subject"]) == []


def test_wire_limit_counts_whitespace_and_accepts_exact_boundary():
    case = example("progress")
    wire = json.dumps(case["payload"]).encode("utf-8")
    padded = wire + b" " * (runtime.MAX_WIRE_BYTES - len(wire))
    assert runtime.validate_wire_message("progress", padded, case["subject"]) == []
    assert json.loads(padded + b" ") == case["payload"]
    assert runtime.validate_wire_message(
        "progress", padded + b" ", case["subject"]
    ) == ["Message exceeds 1 MiB"]


def test_oversized_wire_is_rejected_before_decode_or_parse():
    case = example("progress")
    assert runtime.validate_wire_message(
        "progress", b"\xff" * (runtime.MAX_WIRE_BYTES + 1), case["subject"]
    ) == ["Message exceeds 1 MiB"]


@pytest.mark.parametrize("wire", [b"\xff", b"{invalid", b"\xff\xfe{\x00}\x00"])
def test_wire_requires_utf8_json(wire):
    assert runtime.validate_wire_message(
        "progress", wire, example("progress")["subject"]
    ) == ["Message must be valid UTF-8 JSON"]


@pytest.mark.parametrize(
    "field",
    ["workspace_id", "runtime_id", "runtime_generation", "session_id", "attempt_id"],
)
def test_missing_attempt_identity(field):
    case = example("execute")
    del case["payload"]["target"][field]
    assert errors(case)


@pytest.mark.parametrize(
    "subject",
    [
        "oe.internal.v1.runtime.runtimes.rt_demo.cancel",
        "oe.internal.v1.runtime.runtimes.rt_demo.execute.extra",
        "oe.v1.vcp.control.workspaces.ws_demo.execute",
        "oe.internal.v1.runtime.runtimes.*.execute",
    ],
)
def test_wrong_subject_kind_or_namespace(subject):
    case = example("execute")
    case["subject"] = subject
    assert errors(case)


@pytest.mark.parametrize("value", [0, -1, 1.5, "1"])
def test_invalid_attempt_sequence(value):
    case = example("started")
    case["payload"]["attempt_sequence"] = value
    assert errors(case)


@pytest.mark.parametrize("state", ["starting", "unknown", "terminated", "ready"])
def test_invalid_inspect_state(state):
    case = example("inspect-response")
    case["payload"]["result"]["attempts"][0]["state"] = state
    assert errors(case)


@pytest.mark.parametrize(
    ("request_name", "reply_name", "definition"),
    [
        ("execute", "accepted", "execute"),
        ("cancel", "cancel-accepted", "cancel"),
        ("inspect", "inspect-response", "inspect"),
        ("started", "update-ack", "update"),
    ],
)
def test_reply_must_match_request(request_name, reply_name, definition):
    request, reply = example(request_name), example(reply_name)
    assert (
        runtime.validate_exchange(
            definition, request["payload"], reply["payload"], request["subject"]
        )
        == []
    )
    reply["payload"]["target"]["runtime_generation"] = 2
    assert runtime.validate_exchange(
        definition, request["payload"], reply["payload"], request["subject"]
    )


def test_inspect_cannot_omit_requested_attempt():
    request, reply = example("inspect"), example("inspect-response")
    reply["payload"]["result"]["attempts"] = []
    assert runtime.validate_exchange(
        "inspect", request["payload"], reply["payload"], request["subject"]
    )


def test_reserved_steering_is_not_an_accepted_update():
    case = example("progress")
    case["payload"]["kind"] = "steering.applied"
    assert runtime.validate_message("update", case["payload"], case["subject"])


def test_internal_reference_stays_current():
    from openengine.contracts import reference

    service = "internal/workspace_runtime"
    assert reference.reference_path(service).read_text() == reference.page(service)


def test_restart_preserves_journal_and_advances_runtime_sequence():
    first, restarted = (
        example("ready")["payload"],
        example("ready-restarted")["payload"],
    )
    assert first["target"] == restarted["target"]
    assert first["payload"]["journal_id"] == restarted["payload"]["journal_id"]
    assert first["boot_id"] != restarted["boot_id"]
    assert restarted["sequence"] > first["sequence"]
    assert errors(example("executor-lost")) == []
    assert example("executor-lost")["payload"]["termination"] == "killed"


def test_cancel_before_execute_confirms_no_launch():
    cancel, execute = example("cancel")["payload"], example("execute")["payload"]
    terminal = example("cancelled-before-execute")["payload"]
    assert cancel["target"] == execute["target"] == terminal["target"]
    assert cancel["execution"] == execute["execution"] == terminal["execution"]
    assert terminal["attempt_sequence"] == 1
    assert terminal["termination"] == "never_started"
    assert terminal["cancel_command_id"] == cancel["command_id"]
    assert terminal["cause"] == cancel["cause"]


@pytest.mark.parametrize(
    "value", ["C:/host", "\\host", "a/../secret", "/etc", "a\x00b"]
)
def test_context_cannot_select_host_paths(value):
    case = example("execute")
    case["payload"]["payload"]["context"]["working_directory"] = value
    assert errors(case)


def test_gap_must_request_an_earlier_sequence():
    case = example("event-gap")
    case["payload"]["expected_sequence"] = case["payload"]["sequence"]
    assert errors(case)


@pytest.mark.parametrize(
    ("field", "value"), [("command_id", "rtc_other"), ("sequence", 99)]
)
def test_ack_echoes_identity_and_sequence(field, value):
    request, reply = example("started"), example("update-ack")
    if field == "command_id":
        request, reply = example("execute"), example("accepted")
    reply["payload"][field] = value
    definition = "execute" if field == "command_id" else "update"
    assert runtime.validate_exchange(
        definition, request["payload"], reply["payload"], request["subject"]
    )


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_wire_rejects_non_json_numbers_in_free_form_inputs(constant):
    case = example("execute")
    case["payload"]["payload"]["task"]["inputs"] = {"value": "placeholder"}
    wire = json.dumps(case["payload"]).replace('"placeholder"', constant).encode()
    assert runtime.validate_wire_message("execute", wire, case["subject"]) == [
        "Message must be valid UTF-8 JSON"
    ]


def test_wire_rejects_excessive_json_nesting():
    wire = b"[" * 100_000 + b"0" + b"]" * 100_000
    assert len(wire) < runtime.MAX_WIRE_BYTES
    # Python 3.14's decoder parses this depth, so the schema validator is the
    # layer that runs out of stack; earlier decoders raise RecursionError first.
    assert runtime.validate_wire_message(
        "progress", wire, example("progress")["subject"]
    ) in (
        ["Message must be valid UTF-8 JSON"],
        ["Message exceeds validation nesting limits"],
    )


@pytest.mark.parametrize("number", ["1e400", "-1e400", "1.7976931348623159e308"])
@pytest.mark.parametrize("name", ["usage", "execute"])
def test_wire_rejects_numeric_overflow(number, name):
    case = example(name)
    if name == "usage":
        case["payload"]["payload"]["usage"]["cost_usd"] = "placeholder"
    else:
        case["payload"]["payload"]["task"]["inputs"] = {"value": "placeholder"}
    wire = json.dumps(case["payload"]).replace('"placeholder"', number).encode()
    assert runtime.validate_wire_message(
        case["definition"], wire, case["subject"]
    ) == ["Message must be valid UTF-8 JSON"]


def test_wire_accepts_large_finite_number():
    case = example("usage")
    case["payload"]["payload"]["usage"]["cost_usd"] = 1e308
    assert runtime.validate_wire_message(
        case["definition"], json.dumps(case["payload"]).encode(), case["subject"]
    ) == []


@pytest.mark.parametrize(
    ("start", "end", "valid"),
    [
        ("00.123456789", "00.123456001", False),
        ("00.123456001", "00.123456789", True),
        ("00.123456789", "00.123456789", True),
        ("00.1", "00.100000000", True),
        ("00", "00.000000000", True),
        ("00.999999999", "01", True),
        ("01", "00.999999999", False),
    ],
)
def test_usage_period_preserves_nanosecond_precision(start, end, valid):
    case = example("usage")
    progress = case["payload"]["payload"]
    progress["period_start"] = f"2026-10-22T12:00:{start}Z"
    progress["period_end"] = f"2026-10-22T12:00:{end}Z"
    assert errors(case) == ([] if valid else ["Usage period ends before it starts"])


def test_nested_invalid_instruction_files_return_errors():
    case = example("execute")
    nested = 0
    for _ in range(400):
        nested = [nested]
    case["payload"]["payload"]["context"]["instruction_files"] = [nested, nested]
    wire = json.dumps(case["payload"]).encode()
    assert len(wire) < runtime.MAX_WIRE_BYTES
    assert errors(case)
    assert runtime.validate_wire_message("execute", wire, case["subject"])


def test_schema_recursion_returns_validation_error(monkeypatch):
    class RecursiveValidator:
        def iter_errors(self, payload):
            raise RecursionError
            yield

    monkeypatch.setattr(runtime, "validator", lambda definition: RecursiveValidator())
    case = example("execute")
    assert errors(case) == ["Message exceeds validation nesting limits"]


@pytest.mark.parametrize("count", [101, 4000, 8000])
def test_oversized_references_skip_uniqueness_checks(monkeypatch, count):
    from jsonschema import Draft7Validator

    original = Draft7Validator.VALIDATORS["uniqueItems"]

    def bounded_unique(validator, unique, instance, schema):
        assert not isinstance(instance, list) or len(instance) <= 100
        yield from original(validator, unique, instance, schema)

    monkeypatch.setitem(Draft7Validator.VALIDATORS, "uniqueItems", bounded_unique)
    case = example("execute")
    case["payload"]["payload"]["context"]["references"] = [
        {"name": str(index), "uri": "urn:reference", "sha256": "a" * 64}
        for index in range(count)
    ]
    wire = json.dumps(case["payload"]).encode()
    assert len(wire) < runtime.MAX_WIRE_BYTES
    assert errors(case)
    assert runtime.validate_wire_message("execute", wire, case["subject"])
