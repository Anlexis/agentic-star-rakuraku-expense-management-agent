# CMN-C2-280 - trust-gate boundary tests (unit).
#
# Nodes are invoked via node(state) - BaseNode.__call__ routes the full
# security pipeline (trust gate -> input gate -> execute() -> output
# output gate) - NOT via node.execute(state), which would bypass the gate.
# The rejection test asserts on the RETURNED error dict (__call__ never
# raises for a trust denial).

import json

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.pre_process_node import PreProcessNode


def _base_state(**overrides) -> dict:
    state = {
        "user_input": "Look up the expense report with report number 7042.",
        "input_context": {},
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "correlation_id": "s1-gate-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestTrustGate:
    """PreProcessNode is the single external VERIFIED_EXTERNAL gate."""

    def test_anonymous_caller_is_denied(self):
        node = PreProcessNode()
        state = _base_state(caller_trust_level=TrustLevel.ANONYMOUS.value)
        result = node(state)  # __call__ RETURNS an error dict - never raises
        assert result["status"] == AgentStatus.ERROR.value
        assert any("trust gate denied" in entry for entry in result["error_log"])
        # Denied BEFORE execute() ran: execute-only keys are absent.
        assert "validated_input" not in result
        assert "report_hint" not in result

    def test_verified_external_caller_passes_gate(self):
        node = PreProcessNode()
        result = node(_base_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = json.loads(result["validated_input"])
        assert "report number 7042" in payload["text"]

    def test_inner_nodes_accept_anonymous_caller(self):
        """Inner domain nodes declare ANONYMOUS - the gate lives on pre_process only."""
        from src.nodes.classify_intent_node import ClassifyIntentNode

        node = ClassifyIntentNode()
        state = _base_state(
            caller_trust_level=TrustLevel.ANONYMOUS.value,
            validated_input="Look up the expense report with report number 7042.",
        )
        result = node(state)  # passes the trust gate (ANONYMOUS >= ANONYMOUS)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_expense"

    def test_trust_posture_declarations(self):
        """The single external gate is pre_process; every
        inner domain node (incl. the Rakus API call, which runs under the
        caller's UNELEVATED context) declares ANONYMOUS."""
        from src.nodes.call_rakus_api_node import CallRakusApiNode
        from src.nodes.classify_intent_node import ClassifyIntentNode
        from src.nodes.confirm_node import ConfirmNode
        from src.nodes.infer_rakus_fields_node import InferRakusFieldsNode
        from src.nodes.post_process_node import PostProcessNode
        from src.nodes.validate_input_node import ValidateInputNode

        assert PreProcessNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL
        for node_cls in (
            ValidateInputNode,
            ClassifyIntentNode,
            InferRakusFieldsNode,
            CallRakusApiNode,
            ConfirmNode,
            PostProcessNode,
        ):
            assert node_cls.required_trust_level == TrustLevel.ANONYMOUS, node_cls.__name__

    def test_manifest_trust_matches_pre_process_gate(self):
        """The manifest required_trust_level is the deploy-time declaration of
        the SAME gate PreProcessNode enforces in code - the two must never
        drift. A mismatch denies every real caller at the trust gate, which
        surfaces only once the agent is actually deployed."""
        import pathlib

        import yaml

        manifest_path = pathlib.Path(__file__).parents[2] / "config" / "agent.yaml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        declared = manifest["required_trust_level"]
        assert declared == PreProcessNode.required_trust_level.name == "VERIFIED_EXTERNAL"
