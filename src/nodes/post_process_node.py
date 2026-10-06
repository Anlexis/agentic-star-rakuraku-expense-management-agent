"""Outer post_process node.

Cat 2 outer backbone: finalize the response after the inner Rakus expense
workflow graph has run. GraphNode.merge_output() maps the inner result into the
outer state; this node shapes the caller-facing `formatted_output` and is the
last boundary the response crosses.

The domain output gate is the MODULE-LEVEL `_security_gate_output()` below,
called from execute(). It is deliberately NOT an instance method and NOT a
framework gate hook: the framework's gate methods are @final on FunctionNode
and it auto-wraps the `_extra_` hooks (which breaks the .invoke() chain), so
domain checks live in a module-level helper invoked inline.

**What the ERROR envelope may say: closed-set labels only.** EVERY non-success
return - a gate violation AND a pre-existing inner-workflow error - goes
through the one module-level `_contain()` helper, which clears every
output-bearing field and publishes a constant reason code chosen by this module
(one of `ERROR_REASONS`) and nothing else. Never `error_log`, never the gate's
violation entries, never any other node-authored string: those lines can embed
an upstream Rakus response body, a report number or a caller-derived fragment,
and truncating, path-stripping or credential-only redaction is not a closed
set. `error_log` stays the INTERNAL channel - the state reducer appends to it
and the audit trail needs it - and is simply never projected to the caller.
Gate violations are written to `error_log` naming the offending PATH only, and
the audit event carries the count.

The reason code is a constant, so the envelope stays TRUTHY:
`AgentBaseGraph.get_output()` projects `formatted_output or result` with no
status check, and a falsy envelope would re-open that fallback onto whatever
survived in state.
"""

import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

# Credential-shaped strings that must never reach the caller (defence in depth -
# the framework credential scan in FunctionNode also runs on every result).
_CREDENTIAL_LIKE_RE = re.compile(r"eyJ[A-Za-z0-9._-]{10,}|sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._-]{16,}")

# Stand-in for a path fragment (a mapping KEY) that cannot itself be written
# into a violation label. The label travels in `error_log`, where the
# framework's own credential scan (FunctionNode._security_gate_output, which
# RAISES on a finding in any value of a node result) would refuse this node's
# cleared result and hand back a bare error instead - re-opening the
# `formatted_output or result` fallback the clearing had just closed.
_UNNAMEABLE_KEY = "<withheld>"

# Every state field that can carry released text to the caller. On a gate
# violation ALL of them are cleared, not just the one that tripped: the
# framework's output builder falls back to `result` when `formatted_output` is
# absent, so returning an error status without clearing these would still ship
# the un-gated inner answer inside the error envelope.
_OUTPUT_BEARING_FIELDS = (
    "result",
    "formatted_output",
    "confirmation",
    "expense_title",
    "record_id",
    "record_ref",
    "report_id",
    "approval_status",
    "rakus_payload",
    "intent",
)


def _walk_strings(value: Any, path: str = "") -> "list[tuple[str, str]]":
    """Yield every (path, string) leaf in a nested structure, mapping KEYS included.

    The gate scans the whole rendered structure, not only its top-level string
    values: caller text riding inside a nested mapping (an assembled request
    body, for instance) would otherwise cross the boundary unexamined. A
    mapping's KEYS are yielded for scanning alongside its values, because a
    values-only walk reports zero findings on exactly the case where the
    credential IS the key - and zero findings is indistinguishable from clean.
    """
    found: list[tuple[str, str]] = []
    if isinstance(value, str):
        found.append((path or "<root>", value))
    elif isinstance(value, dict):
        for key, item in value.items():
            key_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str):
                found.append((key_path, key))
            found.extend(_walk_strings(item, key_path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_walk_strings(item, f"{path}[{index}]"))
    return found


def _path_label(path: str) -> str:
    """Render a violation's location with any credential-shaped fragment withheld.

    A path is built from mapping keys, so a credential carried AS a key would
    be quoted by the very label that reports it. Both this module's pattern and
    the framework's detector are applied: the framework's is the floor (it
    matches shapes such as `AKIA…` that the local pattern does not) and it is
    the one that would raise on the node result.
    """
    label = _CREDENTIAL_LIKE_RE.sub(_UNNAMEABLE_KEY, path)
    findings = detect_credentials(label)
    while findings:
        first = findings[0]
        label = label[: first["start"]] + _UNNAMEABLE_KEY + label[first["end"] :]
        findings = detect_credentials(label)
    return label


def _security_gate_output(formatted_output: dict[str, Any], is_success: bool) -> "list[str]":
    """Domain output gate (module-level; called from PostProcessNode.execute()).

    Blocks (returns violations for):
      - a SUCCESS response with no record evidence (record_id/record_ref), which
        would misrepresent the Rakus action outcome to the caller;
      - any credential-shaped string anywhere in the caller-facing output,
        including values nested inside the assembled request body, and in a
        mapping KEY as well as a value.

    A violation names the offending PATH, never the value, and a
    credential-shaped key is withheld from that path (see _path_label).
    Violations are `error_log` entries - internal; they never reach the caller.
    """
    problems: list[str] = []
    if is_success and not (formatted_output.get("record_id") or formatted_output.get("record_ref")):
        problems.append("PostProcess output gate: SUCCESS output missing record evidence")
    for path, text in _walk_strings(formatted_output):
        if _CREDENTIAL_LIKE_RE.search(text):
            # Name the location, never the matched value.
            problems.append(f"PostProcess output gate: credential-like value at '{_path_label(path)}'")
    return problems


# Reason codes - the ONLY values the caller-visible ERROR envelope may carry.
# Chosen here, never derived from state, so the envelope is a closed set: it
# says WHAT happened, never to which record and never in whose words.
_REASON_WORKFLOW_FAILED = "rakus_workflow_failed"  # the inner workflow reported an error
_REASON_OUTPUT_WITHHELD = "output_withheld_by_gate"  # the output gate refused the response
ERROR_REASONS = frozenset({_REASON_WORKFLOW_FAILED, _REASON_OUTPUT_WITHHELD})


def _contain(reason: str, new_errors: "list[str] | None" = None) -> dict[str, Any]:
    """The node result for ANY non-success outcome - the single error shape.

    ERROR status, every output-bearing field cleared, and an envelope made of
    closed-set labels only: `reason` is one of ERROR_REASONS. `new_errors`
    (gate violations - path labels only) go to `error_log`, the internal
    channel the state reducer accumulates, and never enter the envelope.

    Nothing is read out of state: not the record, not `error_log`. The inner
    entries are already in `error_log` and the reducer APPENDS, so re-emitting
    them here would duplicate every line as well as publish it.

    Clearing (not merely omitting) is what withholds the answer: the framework
    builds its output from `formatted_output` or, failing that, `result` -
    regardless of status - so an error return that left the inner answer in
    state would still ship it inside the error envelope. The constant `reason`
    key keeps the mapping TRUTHY for the same reason.
    """
    contained: dict[str, Any] = {field: "" for field in _OUTPUT_BEARING_FIELDS}
    contained["result"] = None
    contained["formatted_output"] = {"reason": reason}
    contained["status"] = AgentStatus.ERROR.value
    if new_errors:
        contained["error_log"] = list(new_errors)
    return contained


class PostProcessNode(FunctionNode):
    """Format the final agent output."""

    # Read-only formatting of the already-produced result.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        errored = state.get("status") == AgentStatus.ERROR.value

        # If the inner workflow errored, preserve the error status (do not mask
        # it) and publish NOTHING of it: the caller receives the reason code
        # only, and the delta clears every output-bearing field so no partial
        # answer rides out and nothing survives for a checkpoint or a
        # downstream reader.
        #
        # Reachability: on the compiled graph AgentBaseGraph.route() sends an
        # ERROR status from `main` straight to `finalize`, and
        # BaseNode.__call__ short-circuits an already-errored state, so this
        # branch is defence in depth for a direct execute() call - and for any
        # future re-wiring.
        if errored:
            # Outcome signals only - a closed-set reason code and a count. The
            # audit log is not a store for expense content or error text.
            emit_trace_event(
                "post_process_error_contained",
                {"reason": _REASON_WORKFLOW_FAILED, "n_errors": len(state.get("error_log", []) or [])},
                state,
            )
            return _contain(_REASON_WORKFLOW_FAILED)

        formatted_output = {
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "expense_title": state.get("expense_title", ""),
            "intent": state.get("intent", ""),
            "approval_status": state.get("approval_status", ""),
            "confirmation": state.get("confirmation", ""),
            "rakus_payload": from_json(state.get("rakus_payload"), {}),
        }

        # A refusal is contained the same way as an inner error: the violations
        # go to error_log only, the caller receives the reason code only.
        violations = _security_gate_output(formatted_output, is_success=True)
        if violations:
            emit_trace_event(
                "post_process_gate_blocked",
                {"reason": _REASON_OUTPUT_WITHHELD, "n_violations": len(violations)},
                state,
            )
            return _contain(_REASON_OUTPUT_WITHHELD, violations)

        # Audit the final response shaping - outcome signals only, no content.
        emit_trace_event(
            "post_process_complete",
            {
                "intent": state.get("intent", ""),
                "has_record_id": bool(state.get("record_id")),
            },
            state,
        )

        return {
            "formatted_output": formatted_output,
            "status": AgentStatus.SUCCESS.value,
        }
