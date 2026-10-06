"""Outer pre_process node.

Cat 2 outer backbone: screen and serialize the caller's request (raw text plus
the structured expense data supplied alongside it) into a single JSON string in
`validated_input`, which the GraphNode (`main` slot) hands to the inner Rakus
expense workflow graph. Business validation happens inside the inner graph's
ValidateInputNode - this node owns the CALLER CONTRACT: it is the boundary
where hostile input either stops or becomes trusted-shape data.

The screens live here, in the node that owns that contract, rather than relying
on a framework gate in front of it: a gate that is absent or configured off
would let the payload through to the answer path and return success, which is
fail-open on exactly the decision this node exists to make.
"""

import json
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.security import (
    InputRejected,
    rejection_notice,
    sanitize_query,
    screen_for_injection,
    screen_structure,
    validate_field_map,
    validate_report_reference,
)

# Caller-supplied keys read from the request context, in resolution order.
_REPORT_KEYS = ("report_number", "report_id", "report_hint")


class PreProcessNode(FunctionNode):
    """Screen and serialize caller input for the inner workflow graph."""

    # The outer backbone's SINGLE external trust gate. A real caller enters at
    # VERIFIED_EXTERNAL and the inner Rakus call runs under this same
    # (unelevated) context, so the external gate lives HERE, not on the inner
    # API node. An under-trusted (ANONYMOUS) caller is denied at this gate
    # before any call.
    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {}) or {}  # read-only

        if not user_input or not user_input.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        raw_text = user_input.strip()
        # Strip markup and cap length, then screen BOTH forms. The strip both
        # deletes control tokens and re-joins split directives, so each form
        # exposes attacks the other hides.
        sanitized_input = sanitize_query(raw_text)

        try:
            screen_for_injection(raw_text, sanitized_input)
            if not isinstance(input_context, dict):
                raise InputRejected("context_type")
            # Depth-first over the parsed structure, keys included, before any
            # field is read individually.
            screen_structure(input_context)
            report_hint = self._resolve_report_hint(input_context)
            fields = validate_field_map(input_context.get("fields"))
        except InputRejected as rejected:
            # The notice names the offending CLASS only - never the value - and
            # it is REBUILT from the exception's typed attributes (a code from
            # the declared REJECTION_CODES set plus this module's own location
            # label) rather than interpolating the caught exception. An
            # interpolated exception is how text from outside a module reaches
            # error_log, which is the audit channel the state reducer
            # accumulates and a checkpoint keeps.
            emit_trace_event(
                "pre_process_rejected",
                {"reason": rejected.code},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: {rejection_notice(rejected.code, rejected.where)}"],
            }

        validated_input = json.dumps({"text": sanitized_input, "report_hint": report_hint, "fields": fields})

        # Audit the shaped request - presence signals only, not the raw text.
        emit_trace_event(
            "pre_process_complete",
            {"has_report_hint": bool(report_hint), "n_caller_fields": len(fields)},
            state,
        )

        return {
            "validated_input": validated_input,
            "report_hint": report_hint,
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _resolve_report_hint(input_context: dict[str, Any]) -> str:
        """Return the caller's report reference, validated to the inert alphabet.

        The target report is caller-supplied and never inferred here. Each
        candidate key is validated, so a malformed reference is a rejection
        rather than a silent fall-through to the next key (which would act on a
        different report than the caller named).
        """
        for key in _REPORT_KEYS:
            if key in input_context and input_context[key] not in (None, ""):
                return validate_report_reference(input_context[key])
        return ""
