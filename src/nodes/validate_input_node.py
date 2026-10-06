"""Inner workflow Step 1: ValidateInput.

Rejects empty / non-request input, re-runs the injection screen as
defence in depth (the inner graph can be driven directly, without the outer
backbone in front of it), and runs a deterministic regex scan of the inbound
text for email addresses / access-token-like strings, which are
flag-and-redacted before anything is logged. An expense request legitimately
names amounts, vendors, and report numbers, so the credential scan is
flag-and-redact for safe logging rather than a hard reject. The injection
screen and the empty/non-request guard are the hard rejects.
"""

import json
import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.security import (
    InputRejected,
    rejection_notice,
    screen_for_injection,
    validate_field_map,
)

# Deterministic patterns: email addresses and bearer/JWT/API-token-like strings
# that might appear in a pasted request. Flagged + redacted before logging.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_TOKEN_RE = re.compile(r"\b(?:eyJ[A-Za-z0-9_-]{6,}|secret_[A-Za-z0-9]{6,}|sk-[A-Za-z0-9]{6,})\b")
_REDACTION = "[REDACTED]"

# Minimum signal that the text is a real request rather than noise.
_MIN_LEN = 3


def _redact(value: str) -> "tuple[str, list[str]]":
    """Return the text with credential-shaped spans removed, plus the classes hit."""
    flags: list[str] = []
    cleaned = value
    if _EMAIL_RE.search(cleaned):
        flags.append("email")
        cleaned = _EMAIL_RE.sub(_REDACTION, cleaned)
    if _TOKEN_RE.search(cleaned):
        flags.append("token")
        cleaned = _TOKEN_RE.sub(_REDACTION, cleaned)
    return cleaned, flags


class ValidateInputNode(FunctionNode):
    """Validate, screen, and flag-and-redact the inbound expense request."""

    # Inner domain node - the external trust gate lives on the outer backbone
    # pre_process (VERIFIED_EXTERNAL); the caller context is forwarded unchanged.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("validated_input") or state.get("user_input") or ""

        # The outer graph serialized the request into a JSON string; accept both
        # the serialized shape and a bare string for direct invocation.
        text = raw
        report_hint = state.get("report_hint", "")
        caller_fields: object = None
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                obj = json.loads(raw)
                text = obj.get("text", "")
                report_hint = obj.get("report_hint", report_hint)
                caller_fields = obj.get("fields")
            except (ValueError, TypeError):
                text = raw

        if not isinstance(text, str) or len(text.strip()) < _MIN_LEN:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ValidateInputNode: empty or non-request input"],
            }

        # Defence in depth: screen again here, so driving the inner graph
        # directly cannot skip the screen the outer node applied. Caller fields
        # arrive already validated as pairs; a raw mapping is re-validated.
        try:
            screen_for_injection(text)
            fields = self._normalize_fields(caller_fields)
        except InputRejected as rejected:
            # Rebuilt from the exception's typed attributes (closed-set code +
            # this module's own location label), never from the caught
            # exception itself - see PreProcessNode for the same reasoning.
            emit_trace_event("validate_input_rejected", {"reason": rejected.code}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"ValidateInputNode: {rejection_notice(rejected.code, rejected.where)}"],
            }

        # Deterministic flag-and-redact (before any logging).
        # Local list per invocation - never a module-global (no cross-invoke leak).
        flags: list[str] = []
        redacted, text_flags = _redact(text)
        flags.extend(text_flags)

        # The SAME redaction runs over the caller's structured fields. The
        # framework's own mask covers only the request-text fields, so free text
        # arriving through the context channel would otherwise reach the logs -
        # and the assembled request body - unredacted.
        clean_fields: list[tuple[str, str]] = []
        for name, value in fields:
            cleaned, field_flags = _redact(value)
            flags.extend(field_flags)
            clean_fields.append((name, cleaned))
        fields = clean_fields
        flags = sorted(set(flags))

        # Audit the scan outcome - redaction flags only, never the inbound text.
        emit_trace_event(
            "validate_input_complete",
            {
                "has_report_hint": bool(report_hint),
                "redaction_flags": flags,
                "n_caller_fields": len(fields),
            },
            state,
        )

        return {
            "validated_input": redacted.strip(),
            "report_hint": report_hint,
            "caller_fields": to_json(fields),
            "redaction_flags": to_json(flags),
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _normalize_fields(caller_fields: object) -> "list[tuple[str, str]]":
        """Return validated (name, value) pairs from either wire shape.

        The outer node emits a list of pairs; a direct caller may pass the
        original mapping. Both go through the same bounds check.
        """
        if caller_fields is None:
            return []
        if isinstance(caller_fields, dict):
            return validate_field_map(caller_fields)
        if isinstance(caller_fields, list):
            pairs = {}
            for entry in caller_fields:
                if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                    raise InputRejected("fields_shape")
                pairs[entry[0]] = entry[1]
            return validate_field_map(pairs)
        raise InputRejected("fields_type")
