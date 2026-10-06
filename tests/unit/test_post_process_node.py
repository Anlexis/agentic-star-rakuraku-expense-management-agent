# CMN-C2-280 - Unit tests: PostProcessNode (outer backbone, domain output gate)
#
# Canon: invoked via node(state), so the full security pipeline runs (trust
# gate -> input gate -> execute -> output gate); this backbone formatter
# declares ANONYMOUS -> the state builder sets
# caller_trust_level = TrustLevel.ANONYMOUS.value. The domain output gate is the
# MODULE-LEVEL _security_gate_output() helper (fleet-green pattern - the
# framework gate methods are @final and the real SDK auto-wraps _extra_ hooks),
# so the helper is also unit-tested directly as a plain function.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import PostProcessNode, _security_gate_output
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "record_id": "7042",
        "record_ref": "rakus://expenses/7042",
        "expense_title": "Expense report 7042",
        "approval_status": "",
        "intent": "lookup_expense",
        "confirmation": "Retrieved expense report 'Expense report 7042' - ref=rakus://expenses/7042 - id=7042",
        "rakus_payload": to_json({"report_number": "7042"}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "post-process-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_success_formats_output(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        assert out["record_id"] == "7042"
        assert out["record_ref"] == "rakus://expenses/7042"
        assert out["intent"] == "lookup_expense"
        assert out["confirmation"].startswith("Retrieved expense report")
        # Round-trip: the JSON rakus_payload string surfaces parsed.
        assert out["rakus_payload"] == {"report_number": "7042"}

    def test_status_is_plain_string_not_enum(self):
        """Regression guard: State status must be the `.value` string,
        never a bare AgentStatus enum member (str-enum equality masks the
        difference in `==` asserts, so pin the concrete type here)."""
        result = self.node(_state())
        # isinstance() is wrong here on purpose: AgentStatus is a str-enum, so
        # isinstance(member, str) is True for the very value this guards against.
        assert type(result["status"]) is str  # noqa: E721
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_error_status_preserved(self):
        """Inner-workflow error must not be masked as success. Real-SDK
        pipeline behavior: BaseNode.__call__ short-circuits on an incoming
        errored state (execute() is skipped), so the error status + error_log
        pass through untouched and no success shape is fabricated."""
        state = _state(
            status=AgentStatus.ERROR.value,
            record_id="",
            record_ref="",
            error_log=["CallRakusApiNode: Rakus API error 403: forbidden"],
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "Rakus API error 403" in "\n".join(result["error_log"])
        assert "formatted_output" not in result

    def test_error_status_as_string_value_preserved(self):
        """The framework may carry status as the enum .value (string) at the boundary."""
        result = self.node(_state(status=AgentStatus.ERROR.value, error_log=["boom"]))
        assert result["status"] == AgentStatus.ERROR.value
        assert "formatted_output" not in result

    def test_output_gate_blocks_success_without_record_evidence(self):
        """Full node path: a SUCCESS output missing record evidence is blocked."""
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("record evidence" in entry for entry in result["error_log"])


class TestSecurityGateOutputHelper:
    """The module-level domain output gate as a plain function (not a node call)."""

    def test_passes_success_with_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "7042", "record_ref": "rakus://expenses/7042", "confirmation": "ok"},
            is_success=True,
        )
        assert violations == []

    def test_blocks_success_without_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "", "record_ref": "", "confirmation": "looks done"},
            is_success=True,
        )
        assert len(violations) == 1
        assert "record evidence" in violations[0]

    def test_blocks_credential_shaped_value(self):
        # Built at runtime so no credential-shaped literal is committed.
        bearer_like = "Bearer " + "a" * 24
        violations = _security_gate_output(
            {"record_id": "7042", "note": bearer_like},
            is_success=True,
        )
        assert any("note" in v for v in violations)

    def test_error_output_not_required_to_carry_evidence(self):
        violations = _security_gate_output({"record_id": "", "record_ref": ""}, is_success=False)
        assert violations == []
