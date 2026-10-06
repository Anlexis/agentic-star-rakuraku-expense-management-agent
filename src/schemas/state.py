"""CMN-C2-280 Rakus Expense Agent state."""

# State must be a flat TypedDict - never a Pydantic BaseModel. Checkpoints are
# serialized with msgpack; Pydantic objects (and nested dict/list containers)
# are not msgpack-safe. Extend AgentState with agent-specific fields only, and
# declare every domain field NotRequired[...] (fields are absent until their
# producer node writes them). rakus_payload / rakus_config / redaction_flags
# are dicts/lists at the point of use but are stored in State as JSON strings
# via to_json/from_json below. Do NOT add credentials, secrets, or Pydantic
# models. The Rakus integration token is NEVER stored here - it is read via
# ctx.secrets in CallRakusApiNode.

from __future__ import annotations

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a list/dict State value to a compact JSON string (msgpack-safe).

    Returns None for None so the field stays a true Optional[str].
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: Any, default: Any) -> Any:
    """Deserialize a JSON-string State value back to its list/dict form.

    Tolerant by design: None/empty -> default; an already-native list/dict (e.g. a value
    supplied directly in a unit test) passes through unchanged; a malformed string -> default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class State(AgentState):
    """Rakus Expense agent state.

    Shared fields (user_input, validated_input, intent, result, status,
    formatted_output, session_id, node_history, error_log, correlation_id,
    trace_id, hitl_*, etc.) are inherited from AgentState and NOT re-declared.
    Only Rakus-workflow fields are added below, all NotRequired. All values are
    JSON/msgpack-serializable primitives - the Rakus integration token is NEVER
    stored here (accessed via ctx.secrets).
    """

    # Caller-supplied target hint (expense report number from input_context /
    # the request envelope). Never inferred; resolution to a Rakus report
    # number is explicit-only (pass-through when the hint or request text
    # already carries a number). Validated to the inert identifier alphabet
    # before it is stored, so it can never carry free text into the output.
    report_hint: NotRequired[str]
    report_id: NotRequired[str]  # resolved Rakus expense report number

    # ValidateInput deterministic scan: JSON list[str] of pattern classes
    # redacted from the text before logging (stored as a JSON string;
    # (de)serialize via to_json/from_json).
    redaction_flags: NotRequired[Optional[str]]

    # Structured expense fields supplied by the caller alongside the request
    # text, validated to bounded inert (name, value) pairs at the boundary.
    # JSON string; (de)serialize via to_json/from_json.
    caller_fields: NotRequired[Optional[str]]

    # InferRakusFields
    expense_title: NotRequired[str]  # expense report title / record label
    # JSON - assembled Rakus expense API request body (stored as a JSON string,
    # not a native dict; (de)serialize via to_json/from_json).
    rakus_payload: NotRequired[Optional[str]]

    # Runtime `rakus:` section forwarded by _parent_config() and injected by
    # the inner graph's _extra_initial_state() (JSON string).
    rakus_config: NotRequired[Optional[str]]
    # Per-call Rakus budget in seconds, from the same runtime config.
    rakus_timeout_s: NotRequired[float]

    # CallRakusApi
    record_id: NotRequired[str]  # report number / record id returned by Rakus
    record_ref: NotRequired[str]  # human-readable reference (rakus://expenses/<number>)
    approval_status: NotRequired[str]  # approval workflow status (check_approval)

    # Confirm
    confirmation: NotRequired[str]  # human-readable confirmation message
