# CMN-C2-280 - Unit tests: CallRakusApiNode (inner Step 4, tool side-effect)
#
# Canon: invoked via node(state), so the full security pipeline runs (trust
# gate -> input gate -> execute -> output gate); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.
# The ONE documented exception: the config-override call passes a 2nd (config)
# argument, which __call__ cannot forward - that single test stays a DIRECT
# execute(state, config=...) call (ANONYMOUS node, trust gate unaffected).
#
# The node builds its client locally (SDK v1 nodes are no-arg), so error-path
# transports are exercised by monkeypatching the module's RakusClient symbol
# (our own module attribute - never a sys.modules stub of shared.*).

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.nodes.call_rakus_api_node import CallRakusApiNode
from src.services.rakus_client import RakusApiError
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.call_rakus_api_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "rakus_payload": to_json({"report_number": "7042"}),
        "intent": "lookup_expense",
        "report_id": "7042",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "call-rakus-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class _FakeErrorClient:
    """Stands in for RakusClient: lookup raises the documented API error."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def find_expense(self, report_number, api_token):
        raise RakusApiError(403, "forbidden by integration permissions")


class _FakeEmptyClient:
    """Stands in for RakusClient: lookup returns no matching report."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def find_expense(self, report_number, api_token):
        return {"expense_data": []}


class _FakeLiveClient:
    """Stands in for RakusClient with a LIVE (non-stub) transport."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = False
    timeout_s = 30.0

    def find_expense(self, report_number, api_token):
        _FakeLiveClient.captured = {"report_number": report_number, "api_token": api_token}
        return {"expense_data": [{"number": report_number, "title": f"Expense report {report_number}"}]}


class TestCallRakusApiNode:
    def setup_method(self):
        self.node = CallRakusApiNode()

    def test_lookup_success_via_default_v1_stub(self):
        # Default transport = deterministic, network-free stub; no secret
        # provider bound -> the node runs on the documented stub placeholder.
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "7042"
        assert result["record_ref"] == "rakus://expenses/7042"
        assert result["report_id"] == "7042"
        assert result["expense_title"] == "Expense report 7042"

    def test_register_success_via_default_v1_stub(self):
        state = _state(
            intent="register_expense",
            report_id="9002",
            expense_title="Taxi",
            rakus_payload=to_json({"expense_data": [{"number": "9002", "title": "Taxi"}]}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "9002"
        assert result["record_ref"] == "rakus://expenses/9002"

    def test_check_approval_success_via_default_v1_stub(self):
        state = _state(intent="check_approval")
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "7042"
        assert result["approval_status"] == "pending"

    def test_rakus_config_state_field_sets_base_url(self):
        # The inner graph injects the manifest `rakus:` section as the JSON
        # rakus_config state field; the stub transport still serves the call.
        state = _state(rakus_config=to_json({"base_url": "https://rakus.example.test/api1"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_ref"] == "rakus://expenses/7042"

    def test_config_override_direct_execute_call(self):
        # Documented canon exception: execute(state, config=...) takes a 2nd
        # argument that __call__ cannot forward, so this ONE test calls execute
        # directly (ANONYMOUS node - the trust gate is not the subject here).
        config = {"configurable": {"rakus": {"base_url": "https://rakus.example.test/api1"}}}
        result = self.node.execute(_state(), config=config)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "7042"

    def test_missing_payload_errors(self):
        result = self.node(_state(rakus_payload=None))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_lookup_with_unresolved_number_errors(self):
        state = _state(report_id="", rakus_payload=to_json({"report_number": ""}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved report number" in entry for entry in result["error_log"])

    def test_check_approval_with_unresolved_number_errors(self):
        state = _state(
            intent="check_approval",
            report_id="",
            rakus_payload=to_json({"report_number": ""}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_lookup_without_matching_report_errors(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_rakus_api_node.RakusClient", _FakeEmptyClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("no expense report found" in entry for entry in result["error_log"])

    def test_unknown_intent_errors(self):
        result = self.node(_state(intent="delete_expense"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unknown intent" in entry for entry in result["error_log"])

    def test_api_error_surfaces_status_error(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_rakus_api_node.RakusClient", _FakeErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("403" in entry for entry in result["error_log"])

    def test_api_error_logs_the_status_and_not_the_upstream_body(self, monkeypatch):
        """The HTTP status is a closed-set label; the response body is not.

        RakusApiError's own message embeds the upstream body, so interpolating
        the caught exception is what puts unbounded third-party text into
        error_log - the channel the audit trail reads and a checkpoint keeps.
        """
        monkeypatch.setattr("src.nodes.call_rakus_api_node.RakusClient", _FakeErrorClient)
        joined = " ".join(self.node(_state())["error_log"])
        assert "Rakus API error 403" in joined
        assert "forbidden by integration permissions" not in joined

    def test_transport_failure_logs_the_exception_class_and_not_its_text(self, monkeypatch):
        class _FakeBrokenClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def find_expense(self, report_number, api_token):
                raise ConnectionError("cannot reach https://tenant.example/api1/expenses for report 7042")

        monkeypatch.setattr("src.nodes.call_rakus_api_node.RakusClient", _FakeBrokenClient)
        joined = " ".join(self.node(_state())["error_log"])
        assert "Rakus call failed (ConnectionError)" in joined
        assert "tenant.example" not in joined
        assert "7042" not in joined

    def test_not_found_reason_names_no_report_number(self, monkeypatch):
        """A report number is caller-derived - it is not a label this node chose."""
        monkeypatch.setattr("src.nodes.call_rakus_api_node.RakusClient", _FakeEmptyClient)
        joined = " ".join(self.node(_state())["error_log"])
        assert "no expense report found" in joined
        assert "7042" not in joined

    def test_unknown_intent_reason_does_not_echo_the_intent(self):
        joined = " ".join(self.node(_state(intent="delete_expense"))["error_log"])
        assert "unknown intent" in joined
        assert "delete_expense" not in joined

    def test_live_transport_without_secret_refuses_call(self, monkeypatch):
        # With a LIVE transport a missing RAKUS_TOKEN is a hard error -
        # a real API is never called unauthenticated.
        monkeypatch.setattr("src.nodes.call_rakus_api_node.RakusClient", _FakeLiveClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unauthenticated" in entry for entry in result["error_log"])

    def test_live_transport_reads_token_from_ctx_secrets(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_rakus_api_node.RakusClient", _FakeLiveClient)
        _FakeLiveClient.captured = {}
        with bound_secrets(InMemoryProvider({"RAKUS_TOKEN": "mock-token-for-testing"})):
            result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _FakeLiveClient.captured["api_token"] == "mock-token-for-testing"
        assert _FakeLiveClient.captured["report_number"] == "7042"

    def test_audit_emits_side_effect_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_rakus_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        payload = payloads["call_rakus_api_complete"]
        assert payload["intent"] == "lookup_expense"
        assert payload["has_record_id"] is True
        assert payload["stub_transport"] is True
