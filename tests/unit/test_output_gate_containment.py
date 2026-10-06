# CMN-C2-280 - Unit tests: the output boundary.
#
# Two properties, each of which failed silently before it was tested:
#
#   1. The gate scans the WHOLE rendered structure. A gate that inspects only
#      top-level string values reports zero findings on caller text nested one
#      level deep, and "zero findings" is indistinguishable from "clean" unless
#      a positive control proves the scanner works at all.
#   2. A violating gate CLEARS the output-bearing fields. The framework builds
#      its response from `formatted_output` or, failing that, `result` -
#      regardless of status - so returning an error while leaving the inner
#      answer in state still ships that answer inside the error envelope.

from framework.schemas.agent_status import AgentStatus

from src.graph.graph import RakusExpenseAgent
from src.nodes.post_process_node import (
    _OUTPUT_BEARING_FIELDS,
    PostProcessNode,
    _security_gate_output,
)

# Built at runtime so no credential-shaped literal is committed to the repo.
_CREDENTIAL_LIKE = "Bearer " + "a" * 24


def _success_state(**overrides):
    """State as it exists right after the inner graph's result is merged in."""
    inner_answer = {
        "record_id": "7042",
        "record_ref": "rakus://expenses/7042",
        "confirmation": f"Retrieved expense report '{_CREDENTIAL_LIKE}'",
    }
    state = {
        "status": AgentStatus.SUCCESS.value,
        "result": inner_answer,
        "record_id": "7042",
        "record_ref": "rakus://expenses/7042",
        "expense_title": _CREDENTIAL_LIKE,
        "intent": "lookup_expense",
        "approval_status": "",
        "confirmation": inner_answer["confirmation"],
        "rakus_payload": None,
    }
    state.update(overrides)
    return state


class TestGateWalksNestedStructures:
    def test_clean_output_produces_no_findings(self):
        """Negative control: without this, an empty result proves nothing."""
        assert _security_gate_output({"record_id": "7042", "record_ref": "r", "confirmation": "ok"}, True) == []

    def test_top_level_credential_is_found(self):
        """Positive control: the scanner itself works."""
        findings = _security_gate_output({"record_id": "7042", "confirmation": _CREDENTIAL_LIKE}, True)
        assert len(findings) == 1
        assert "confirmation" in findings[0]

    def test_nested_credential_is_found(self):
        """The case a top-level-only scan reports as clean."""
        findings = _security_gate_output(
            {
                "record_id": "7042",
                "record_ref": "r",
                "rakus_payload": {"expense_data": [{"items": [{"name": "Note", "values": [_CREDENTIAL_LIKE]}]}]},
            },
            True,
        )
        assert len(findings) == 1
        assert "rakus_payload" in findings[0]

    def test_finding_names_the_location_not_the_value(self):
        findings = _security_gate_output({"record_id": "7042", "confirmation": _CREDENTIAL_LIKE}, True)
        assert _CREDENTIAL_LIKE not in " ".join(findings)


class TestViolationIsContained:
    def test_violation_clears_every_output_bearing_field(self):
        result = PostProcessNode().execute(_success_state())
        assert result["status"] == AgentStatus.ERROR.value
        for field in _OUTPUT_BEARING_FIELDS:
            assert field in result, f"{field} was not cleared"
        assert result["result"] is None

    def test_error_envelope_carries_no_released_text(self):
        """The property that matters: the caller receives none of the answer."""
        state = _success_state()
        merged = {**state, **PostProcessNode().execute(dict(state))}
        envelope = RakusExpenseAgent.get_output(None, merged)
        rendered = repr(envelope)
        assert envelope["status"] == AgentStatus.ERROR.value
        assert _CREDENTIAL_LIKE not in rendered
        assert "Retrieved expense report" not in rendered

    def test_inner_error_is_also_contained(self):
        state = _success_state(status=AgentStatus.ERROR.value, error_log=["inner failed"])
        merged = {**state, **PostProcessNode().execute(dict(state))}
        envelope = RakusExpenseAgent.get_output(None, merged)
        assert envelope["status"] == AgentStatus.ERROR.value
        assert _CREDENTIAL_LIKE not in repr(envelope)

    def test_error_envelope_carries_no_paths_or_credentials(self):
        """Nothing from error_log reaches the caller, whatever the entries say.

        The envelope no longer carries the log at all, so a source path and a
        credential-shaped entry produce the same constant envelope - there is
        nothing left to strip or truncate. The inner entries are not re-emitted
        either: the state reducer appends, so re-emitting would duplicate every
        line as well as publish it.
        """
        result = PostProcessNode().execute(
            {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    "CallRakusApiNode: failed at /srv/app/src/nodes/call_rakus_api_node.py",
                    f"stray {_CREDENTIAL_LIKE}",
                ],
            }
        )
        assert result["formatted_output"] == {"reason": "rakus_workflow_failed"}
        assert "error_log" not in result
        rendered = repr(result)
        assert "/srv/app/src" not in rendered
        assert ".py" not in rendered
        assert _CREDENTIAL_LIKE not in rendered

    def test_clean_success_still_returns_the_output(self):
        """Containment must not swallow a legitimate response."""
        state = _success_state(
            expense_title="Q3 client dinner",
            confirmation="Retrieved expense report 'Q3 client dinner'",
            result={"record_id": "7042"},
        )
        result = PostProcessNode().execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"]["record_id"] == "7042"
