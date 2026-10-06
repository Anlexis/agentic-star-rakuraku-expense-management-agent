# CMN-C2-280 - Unit tests: ConfirmNode (inner Step 5)
#
# Canon: invoked via node(state), so the full security pipeline runs (trust
# gate -> input gate -> execute -> output gate); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.confirm_node import ConfirmNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.confirm_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "record_id": "7042",
        "record_ref": "rakus://expenses/7042",
        "expense_title": "Expense report 7042",
        "approval_status": "",
        "intent": "lookup_expense",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "confirm-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestConfirmNode:
    def setup_method(self):
        self.node = ConfirmNode()

    def test_lookup_confirmation(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Retrieved expense report" in result["confirmation"]
        assert "Expense report 7042" in result["confirmation"]
        assert "ref=rakus://expenses/7042" in result["confirmation"]
        assert "id=7042" in result["confirmation"]
        assert result["result"]["record_id"] == "7042"
        assert result["result"]["record_ref"] == "rakus://expenses/7042"

    def test_register_verb(self):
        result = self.node(
            _state(
                intent="register_expense", record_id="9002", record_ref="rakus://expenses/9002", expense_title="Taxi"
            )
        )
        assert "Registered expense report" in result["confirmation"]

    def test_check_approval_verb_and_status(self):
        result = self.node(_state(intent="check_approval", approval_status="pending"))
        assert "Checked approval status of expense report" in result["confirmation"]
        assert "approval=pending" in result["confirmation"]

    def test_unknown_intent_uses_generic_verb(self):
        result = self.node(_state(intent="mystery"))
        assert "Processed expense report" in result["confirmation"]

    def test_id_only_no_ref(self):
        result = self.node(_state(record_ref=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "id=7042" in result["confirmation"]
        assert "ref=" not in result["confirmation"]

    def test_falls_back_to_record_id_when_title_missing(self):
        result = self.node(_state(expense_title=""))
        assert "'7042'" in result["confirmation"]

    def test_missing_record_evidence_errors(self):
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]
