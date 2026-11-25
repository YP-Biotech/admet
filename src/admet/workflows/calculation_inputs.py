"""Read-only run inputs, event windows and trace integration.

A calculation reads one run folder's run.db: its record for the protocol, the execution, the
events and the measurements, its timeline for the readings. A result records a digest of each
input, so a later change to either marks it outdated.
"""

import hashlib
import json
import math
from pathlib import Path

from admet.core import rundb
from admet.core.runs import FINISHED, RECORD, Run
from admet.workflows.calculation_schema import declarations
from admet.workflows.json_protocol import resolve, validate_measurement


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


TIMELINE_INPUT = "timeline"      # how a result names the run's timeline among its inputs
RECORD_INPUT = "record"


def recording_path(directory, summary=None):
    """The run's database, when it holds a timeline."""
    path = Path(directory) / RECORD
    if not path.is_file() or not rundb.timeline_columns(path):
        raise ValueError("This run has no fluidics recording")
    return path


def result_ref(directory, result_id):
    """How a saved result is named: the run's record and the result's id in it."""
    return f"{Path(directory) / RECORD}#{result_id}"


def load_result(ref):
    """A saved result from its name, with where it lives."""
    record, _, result_id = str(ref).partition("#")
    path = Path(record)
    if path.name != RECORD or not result_id:
        raise ValueError(f"not a saved result: {ref}")
    payload = Run(path.parent).result(result_id)
    if payload is None:
        raise ValueError(f"result not found: {ref}")
    return {**payload, "path": str(ref), "directory": str(path.parent)}


def _result_digest(payload):
    kept = {key: value for key, value in payload.items() if key not in ("path", "directory", "outdated")}
    return hashlib.sha256(json.dumps(kept, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _input_digest(directory, name):
    if "#" in str(name):
        return _result_digest(load_result(name))
    if name == RECORD_INPUT:
        return Run(directory).inputs_digest()
    if name == TIMELINE_INPUT:
        return rundb.timeline_digest(Path(directory) / RECORD)
    raise ValueError(f"not an input of a run: {name}")


def fingerprint(context):
    """A digest of each input the calculation read: the record (results aside), the timeline, the references."""
    directory = context["directory"]
    inputs = [{"path": RECORD_INPUT, "sha256": _input_digest(directory, RECORD_INPUT)},
              {"path": TIMELINE_INPUT, "sha256": _input_digest(directory, TIMELINE_INPUT)}]
    for reference in context.get("references", {}).values():
        inputs.append({"path": reference["path"], "sha256": _result_digest(reference)})
    return inputs


def result_current(payload):
    try:
        directory = Path(payload["directory"]) if payload.get("directory") else Path(str(payload["path"]).partition("#")[0]).parent
        return bool(payload["inputs"]) and all(_input_digest(directory, item["path"]) == item["sha256"]
                                               for item in payload["inputs"])
    except (OSError, KeyError, TypeError, ValueError):
        return False


def load_context(directory, calculation_id, entry, references=None):
    directory = Path(directory).resolve()
    summary = Run(directory).read()
    if not summary:
        raise ValueError("Choose a recorded run")
    if summary.get("state") not in FINISHED:
        raise ValueError("Wait until the recording has finished before calculating")
    if summary.get("artifacts", {}).get("recording_closed") is False:
        raise ValueError("This recording was not successfully closed; do not analyze an active file")
    timeline = recording_path(directory)
    document = (summary.get("normalized_settings") or {}).get("protocol")
    resolved = resolve(document) if document and "parameters" in document else document
    config = next((item for item in declarations(resolved or {}) if item.get("type") == calculation_id), {})
    if calculation_id in {"gravimetry", "dead_volume", "viscosity"}:
        verify_execution(document, summary)
    measurements = {"values": {}, "fields": {}, "revision": None}
    if document and document.get("measurements"):
        measurements = summary.get("measurements") or {}
        if measurements.get("fields") != resolved.get("measurements"):
            raise ValueError("Saved measurement declarations disagree with executed protocol")
        for key, field in measurements["fields"].items():
            value = measurements["values"].get(key)
            validate_measurement(value, field)
            if field.get("required") and value is None and calculation_id != "recording_summary":
                raise ValueError(f"Missing measurement: {field['label']}")
    loaded_references = {}
    for role, value in (references or {}).items():
        expected = entry.get("references", {}).get(role)
        if expected is None:
            raise ValueError(f"Unsupported reference: {role}")
        payload = load_result(value)
        if payload.get("calculation_id") != expected or not result_current(payload):
            raise ValueError(f"{role}: incompatible or outdated reference result")
        if payload["result"].get("status") not in {"complete", "consistent", "usable"}:
            raise ValueError(f"{role}: reference result is inconclusive")
        loaded_references[role] = payload
    return {"directory": directory, "summary": summary, "document": document, "timeline": timeline,
            "config": config, "measurements": measurements, "references": loaded_references,
            "events": [event for event in summary.get("events", []) if isinstance(event, dict)]}


def verify_execution(document, summary):
    from admet.engines.acquisition.pipeline import expand_protocol_steps
    from admet.workflows.operations import _step_from

    resolved = resolve(document)
    expanded = expand_protocol_steps([_step_from(step, i) for i, step in enumerate(resolved["steps"])])
    recorded = summary.get("steps", [])
    if len(recorded) != len(expanded):
        raise ValueError("Recorded execution steps are missing or disagree with protocol")
    for saved, step in zip(recorded, expanded):
        expected = {"flow_setpoints_ul_min": {str(k): v for k, v in step.sensor_setpoints.items()},
                    "pressure_setpoints_mbar": {str(k): v for k, v in step.pressure_setpoints.items()},
                    "trigger_type": step.trigger_type, "trigger_params": step.trigger_params,
                    "timeout_s": step.timeout_s if step.timeout_s is not None else step.trigger_params.get("timeout_s"),
                    "on_complete": step.on_complete, "confirmation": step.confirm_message or None}
        if any(saved.get(key) != value for key, value in expected.items()):
            raise ValueError("Recorded execution and protocol parameters disagree")
    archived = summary.get("normalized_settings", {}).get("protocol")
    if archived is None or resolve(archived) != resolved:
        raise ValueError("Calculation declarations disagree with executed protocol")


def _origin(context):
    origin = context["summary"].get("artifacts", {}).get("polling_origin_monotonic")
    if not isinstance(origin, (int, float)) or not math.isfinite(origin) or origin <= 0:
        raise ValueError("Missing recording clock origin")
    return origin


def skipped_steps(context):
    """The one-based steps the operator skipped during the run."""
    return {e["step_index"] + 1 for e in context["events"]
            if e.get("outcome") == "skipped" and type(e.get("step_index")) is int}


def _step_events(context, step):
    return [e for e in context["events"] if e.get("step_name") and e.get("step_index") == step - 1]


def _flow_starts(events):
    return [e for e in events if e.get("outcome") == "running" and e.get("state") == "running"
            and not e.get("confirmation_message")]


def step_window(context, step, settle_s=0):
    if context["summary"]["state"] != "completed":
        raise ValueError("Protocol did not complete")
    origin = _origin(context)
    events = _step_events(context, step)
    starts = _flow_starts(events)
    ends = [e for e in events if e.get("outcome") == "completed"]
    if (not starts or len(ends) != 1 or any(e.get("state") == "paused"
            or e.get("outcome") in {"skipped", "cancelled", "error"} for e in events)):
        raise ValueError(f"Step {step}: missing, paused, skipped or incomplete event window")
    start, end = starts[0].get("monotonic"), ends[0].get("monotonic")
    if any(type(t) not in (int, float) or not math.isfinite(t) for t in (start, end)) or end <= start + settle_s:
        raise ValueError(f"Step {step}: invalid timing")
    return start - origin + settle_s, end - origin


def settled_after(context, channel, step, end, setpoint, *, hold_s=0.5, limit_s=10.0):
    """When the flow still arriving after a step's stop has died down, in recording seconds.

    Settled uses the live tail rule: within max(0.5, 5 % of the setpoint) of zero
    for hold_s. Never later than limit_s after the stop or the next step's flow start.
    """
    upper = end + limit_s
    following = [e.get("monotonic") for e in _flow_starts(_step_events(context, step + 1))]
    if following and type(following[0]) in (int, float) and math.isfinite(following[0]):
        upper = min(upper, following[0] - _origin(context))
    band = max(0.5, 0.05 * abs(setpoint))
    quiet = None
    for t, q in _read_rows(context, [f"flow_{channel}_ul_min"]):
        if t <= end:
            continue
        if t > upper:
            raise ValueError(f"Flow did not settle within {upper - end:.3g} s after the stop")
        if math.isfinite(q) and abs(q) <= band:
            quiet = t if quiet is None else quiet
            if t - quiet >= hold_s:
                return quiet
        else:
            quiet = None
    raise ValueError("Recording ends before the flow settled after the stop")


def _read_rows(context, columns):
    rows = []
    for row in rundb.timeline_rows(context["timeline"]):
        try:
            t = float(row["elapsed_s"])
            values = [float(row.get(column, "")) for column in columns]
        except (ValueError, TypeError, KeyError):
            try:
                t = float(row["elapsed_s"])
            except (ValueError, TypeError, KeyError):
                raise ValueError("Recording has invalid timestamps") from None
            values = [math.nan] * len(columns)
        rows.append((t, *values))
    if any(not math.isfinite(row[0]) for row in rows) or any(b[0] <= a[0] for a, b in zip(rows, rows[1:])):
        raise ValueError("Recording timestamps are not strictly increasing")
    return rows


def trace(context, channel, lower, upper, *, pressure=False):
    columns = [f"flow_{channel}_ul_min"] + ([f"pressure_{channel}_mbar"] if pressure else [])
    rows = _read_rows(context, columns)
    inside = [i for i, row in enumerate(rows) if lower <= row[0] <= upper]
    if not inside:
        raise ValueError("No recorded samples in measurement window")
    first, last = inside[0], inside[-1]
    if rows[first][0] > lower:
        first = max(0, first - 1)
    if rows[last][0] < upper:
        last = min(len(rows) - 1, last + 1)
    selected = rows[first:last + 1]
    if (selected[0][0] > lower or selected[-1][0] < upper
            or any(not all(math.isfinite(v) for v in row) for row in selected)
            or any(b[0] - a[0] > 1 for a, b in zip(selected, selected[1:]))):
        raise ValueError("Missing samples or insufficient coverage of measurement window")
    def interpolate(t):
        for a, b in zip(selected, selected[1:]):
            if a[0] <= t <= b[0]:
                return (t, *(x + (y - x) * (t - a[0]) / (b[0] - a[0]) for x, y in zip(a[1:], b[1:])))
        raise ValueError("Cannot align recording boundary")
    return [interpolate(lower), *(row for row in selected if lower < row[0] < upper), interpolate(upper)]


def volume_ul(rows):
    return sum((b[0] - a[0]) * (a[1] + b[1]) / 120 for a, b in zip(rows, rows[1:]))
