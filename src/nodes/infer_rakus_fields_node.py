"""Inner workflow Step 3: InferRakusFields.

Assembles a validated Rakus (RakuRaku Seisan) expense API request body for the
classified intent from two sources: the structured fields the caller supplied
alongside the request, and the "Key: value" lines parsed out of the request
text itself. Caller-supplied fields take precedence - they arrived already
bound to an inert shape, whereas text-parsed fields are a best-effort read of
free prose.

The report number is taken only from an explicit number in the text or the
caller-supplied reference - an unresolved number is left empty rather than
invented, so a request can never act on the wrong expense report; the executor
surfaces the miss as status=error. Field extraction is deterministic
(regex/line structure), so the step runs without a model.
"""

import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json, to_json

# A Rakus expense report number: short alphanumeric identifier (no spaces).
_CODE_SHAPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")
# Explicit report-number mention in the request text, EN or JA
# ("report number 7042" / "expense id: E-12" / "申請番号 7042").
_CODE_IN_TEXT_RE = re.compile(
    r"(?:report|expense|slip|application)\s+(?:number|id|code)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})"
    r"|(?:申請番号|伝票番号|精算番号|報告書番号)\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})",
    re.IGNORECASE,
)
# Quoted title: titled "Foo" / named "Foo". Curly quotes as \u escapes so
# the source stays pure ASCII (push-safe).
_TITLE_QUOTED_RE = re.compile(r'(?:titled|named|called|for)\s+["“]([^"”\n]+)["”]', re.IGNORECASE)
# "Key: value" expense-field lines (ASCII or full-width colon). CJK ranges:
# hiragana/katakana + CJK unified ideographs, as \u escapes (push-safe).
_KV_RE = re.compile(r"^\s*([A-Za-z぀-ヿ一-鿿][\w \-぀-ヿ一-鿿]{0,40})[:：]\s*(.+?)\s*$")
# Keys that are the report number/title themselves, not custom expense fields.
_CODE_KEYS = ("number", "report number", "expense number", "slip number", "id")
_TITLE_KEYS = ("title", "subject", "purpose")


class InferRakusFieldsNode(FunctionNode):
    """Extract entities and assemble the Rakus expense API request body."""

    # Inner domain node - derives fields from already-validated data; the
    # external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        text = state.get("validated_input", "") or ""
        intent = state.get("intent", "lookup_expense") or "lookup_expense"
        report_hint = state.get("report_hint", "") or ""

        if not text.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InferRakusFieldsNode: missing validated_input"],
            }

        # Caller-supplied fields first (already bounded and inert), then any
        # additional fields parsed from the request text.
        caller_fields = [(str(name), str(value)) for name, value in from_json(state.get("caller_fields"), []) or []]
        fields = caller_fields + [
            pair for pair in self._parse_fields(text) if pair[0] not in {name for name, _ in caller_fields}
        ]
        report_id = self._resolve_number(text, report_hint, fields)
        expense_title = self._resolve_title(text, fields)

        if intent == "register_expense":
            payload = self._build_expense_data(report_id, expense_title, fields)
        else:  # lookup_expense / check_approval (read-only)
            payload = {"report_number": report_id}

        # Audit the assembled payload shape - field signals only, not content.
        emit_trace_event(
            "infer_rakus_fields_complete",
            {
                "intent": intent,
                "has_report_id": bool(report_id),
                "n_fields": len(fields),
                "n_caller_fields": len(caller_fields),
            },
            state,
        )

        return {
            "report_id": report_id,
            "expense_title": expense_title,
            "rakus_payload": to_json(payload),
            "status": AgentStatus.SUCCESS.value,
        }

    # -- extraction -----------------------------------------------------------

    def _resolve_number(self, text: str, report_hint: str, fields: "list[tuple[str, str]]") -> str:
        """Explicit number only: text mention > number-shaped hint > 'Number:' field. Never invented."""
        m = _CODE_IN_TEXT_RE.search(text)
        if m:
            return m.group(1) or m.group(2) or ""
        hint = report_hint.strip()
        if hint and _CODE_SHAPE_RE.match(hint):
            return hint
        for key, value in fields:
            if key.strip().lower() in _CODE_KEYS and _CODE_SHAPE_RE.match(value.strip()):
                return value.strip()
        return ""  # unresolved - left empty, never invented

    def _resolve_title(self, text: str, fields: "list[tuple[str, str]]") -> str:
        m = _TITLE_QUOTED_RE.search(text)
        if m:
            return m.group(1).strip()[:100]
        for key, value in fields:
            if key.strip().lower() in _TITLE_KEYS:
                return value.strip()[:100]
        return ""

    def _parse_fields(self, text: str) -> "list[tuple[str, str]]":
        """Return the [(key, value), ...] expense fields parsed from the request lines."""
        fields: list[tuple[str, str]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _KV_RE.match(stripped)
            if m:
                fields.append((m.group(1).strip(), m.group(2).strip()))
        return fields

    # -- payload assembly (Rakus expense_data shape) ---------------------------

    def _build_expense_data(
        self, report_id: str, expense_title: str, fields: "list[tuple[str, str]]"
    ) -> dict[str, Any]:
        record: dict[str, Any] = {"number": report_id}
        if expense_title:
            record["title"] = expense_title
        items = []
        for key, value in fields:
            if key.strip().lower() in _CODE_KEYS + _TITLE_KEYS:
                continue
            items.append({"name": key, "values": [value]})
        if items:
            record["items"] = items
        return {"expense_data": [record]}
