# CMN-C2-280 - Unit tests: InferRakusFieldsNode (inner Step 3)
#
# Canon: invoked via node(state), so the full security pipeline runs (trust
# gate -> input gate -> execute -> output gate); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.
# Positive payloads are PII-free: the framework input mask rewrites Title-Case
# bigrams in validated_input, so quoted titles use a single-word label.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.infer_rakus_fields_node import InferRakusFieldsNode
from src.schemas.state import from_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.infer_rakus_fields_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str, intent: str = "lookup_expense", report_hint: str = "", **overrides) -> dict:
    state = {
        "validated_input": text,
        "intent": intent,
        "report_hint": report_hint,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "infer-fields-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestInferRakusFieldsNode:
    def setup_method(self):
        self.node = InferRakusFieldsNode()

    def test_lookup_extracts_number_from_text(self):
        result = self.node(
            _state("Look up the expense report with report number 7042 and summarize the record on file.")
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["report_id"] == "7042"
        # rakus_payload is stored as a JSON string, not a native dict.
        assert isinstance(result["rakus_payload"], str)
        assert from_json(result["rakus_payload"], {}) == {"report_number": "7042"}

    def test_number_shaped_hint_used_when_text_has_no_number(self):
        result = self.node(_state("Show the current expense summary", report_hint="E-123"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["report_id"] == "E-123"
        assert from_json(result["rakus_payload"], {}) == {"report_number": "E-123"}

    def test_check_approval_builds_readonly_payload(self):
        result = self.node(
            _state("Has expense report 7042 been approved yet? See expense number 7042.", intent="check_approval")
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert from_json(result["rakus_payload"], {}) == {"report_number": "7042"}

    def test_register_builds_expense_data_payload(self):
        # Field values stay lower-case: the framework name mask rewrites
        # Title-Case word pairs even ACROSS newlines ("Category\nAmount" style
        # pairs would be masked before execute() sees the text).
        text = 'Register a new expense titled "Taxi"\nCategory: travel\nAmount: 4200'
        result = self.node(_state(text, intent="register_expense", report_hint="9002"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["report_id"] == "9002"
        assert result["expense_title"] == "Taxi"
        payload = from_json(result["rakus_payload"], {})
        record = payload["expense_data"][0]
        assert record["number"] == "9002"
        assert record["title"] == "Taxi"
        assert {"name": "Category", "values": ["travel"]} in record["items"]
        assert {"name": "Amount", "values": ["4200"]} in record["items"]

    def test_unresolved_number_left_empty_never_invented(self):
        result = self.node(_state("Show the expense summary for the flagged record"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["report_id"] == ""
        assert from_json(result["rakus_payload"], {}) == {"report_number": ""}

    def test_non_number_shaped_hint_left_unresolved(self):
        result = self.node(_state("Show the expense summary", report_hint="not a valid number!"))
        assert result["report_id"] == ""

    def test_missing_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]
