# PB: the caller-visible ERROR envelope carries closed-set labels only (CMN-C2-280).
#
# Sibling axis to test_output_gate_containment.py. That file asks whether the
# ERROR return CLEARS the answer; this one asks whose WORDS the envelope is
# made of.
#
# molt source review, 2026-09-04 (family-level finding across the Wave 9 CMN
# family): "That log can contain upstream exception/response text; truncation,
# path stripping, or credential-only redaction is not a closed-set error
# contract. Identifiers, names, emails, and arbitrary third-party response
# bodies remain possible. The caller-visible error must instead be closed-set
# labels only."
#
# The defect these tests hold closed: PostProcessNode built the envelope as
# {"error": [scrubbed error_log or the gate's violations]} and returned it under
# `formatted_output`. Every output-bearing field WAS cleared and the envelope
# WAS record-free - and the caller still received the node-authored lines,
# because AgentBaseGraph.get_output() projects `formatted_output or result`
# with no status check. Clearing the answer is a different property from
# bounding the error channel, and path-stripping + length-capping the lines is
# not a closed set.
#
# The contract now: every non-success return is `_contain()`, and the envelope
# is {"reason": r} with r drawn from ERROR_REASONS. `error_log` stays the
# internal channel - the state reducer accumulates it and the audit trail needs
# it - and is never projected.
#
# Reachability, stated plainly because it decides what each test can prove:
#   * the INNER-ERROR branch of execute() is defence in depth on the real
#     wheel. AgentBaseGraph.route() sends an ERROR status from `main` straight
#     to `finalize` and BaseNode.__call__ short-circuits an already-errored
#     state, so post_process never runs on it. It is driven here by a direct
#     execute() call - which is also how any future re-wiring would reach it.
#     The end-to-end test below covers the same path at the invoke surface,
#     where the envelope is simply never built.
#   * the GATE-VIOLATION branch IS reachable end to end, and the ASGI test
#     drives it through the real /invoke.

import json

import pytest

try:
    from fastapi.testclient import TestClient

    from framework.schemas.agent_status import AgentStatus

    from src.nodes.post_process_node import (
        ERROR_REASONS,
        PostProcessNode,
        _REASON_OUTPUT_WITHHELD,
        _REASON_WORKFLOW_FAILED,
    )
    from src.schemas.state import to_json

    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - only when the framework wheel is absent
    _IMPORT_ERROR = exc

pytestmark = pytest.mark.skipif(_IMPORT_ERROR is not None, reason=f"framework wheel unavailable: {_IMPORT_ERROR}")

# A recognisable stand-in for the shape molt named: an upstream response body,
# quoted into a node's error message, carrying a person and a record. `_MARKER`
# is a lowercase alphanumeric token no detector rewrites, so the platform PII
# filter cannot mask it before an assertion can see it - a masked sentinel
# would make every absence assertion pass vacuously.
_MARKER = "zzmarker4d9f1a"
_SENTINEL = "CallRakusApiNode: upstream said {'employee': '" + _MARKER + "', 'report': '7042'}"

# Assembled at runtime so no credential-shaped literal is committed to the tree
# (the blocking check_credentials gate scans the whole repository).
_BEARER_LIKE = "Bearer " + "a" * 24


def _strings(value, path="output"):
    """Every (path, string) in a nested structure - mapping KEYS included.

    Keys are walked because a leak can ride one: an envelope keyed by
    node-authored text, or a violation label built from a caller-influenced
    key, is invisible to a values-only scan.
    """
    found = []
    if isinstance(value, str):
        found.append((path, value))
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                found.append((f"{path}.<key>", key))
            found.extend(_strings(item, f"{path}.{key}"))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_strings(item, f"{path}[{index}]"))
    else:
        found.append((path, str(value)))
    return found


def _assert_absent(needle, value, where):
    for path, text in _strings(value):
        assert needle not in text, f"{needle!r} reached {where} at {path}"


class TestTheWalkerItself:
    """Verify the verifier: an absence assertion proves nothing if the walk is blind."""

    def test_a_nested_value_is_reached(self):
        with pytest.raises(AssertionError):
            _assert_absent(_MARKER, {"a": [{"b": _SENTINEL}]}, "probe")

    def test_a_nested_key_is_reached(self):
        with pytest.raises(AssertionError):
            _assert_absent(_MARKER, {"a": {_MARKER: "x"}}, "probe")

    def test_a_non_string_leaf_is_reached(self):
        with pytest.raises(AssertionError):
            _assert_absent("7042", {"a": 7042}, "probe")

    def test_a_clean_structure_passes(self):
        _assert_absent(_MARKER, {"reason": "output_withheld_by_gate"}, "probe")


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _base_state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "result": {"record_id": "7042", "confirmation": "Retrieved expense report '7042'"},
        "record_id": "7042",
        "record_ref": "rakus://expenses/7042",
        "expense_title": "Q3 client dinner",
        "approval_status": "",
        "intent": "lookup_expense",
        "confirmation": "Retrieved expense report 'Q3 client dinner' - ref=rakus://expenses/7042 - id=7042",
        "rakus_payload": to_json({"report_number": "7042"}),
        "correlation_id": "pb-closed-set",
        "node_history": [],
        # EVERY path below starts with the sentinel already in the internal
        # channel, so each one is a real test of whether that channel leaks.
        "error_log": [_SENTINEL],
        "execution_time": {},
    }
    state.update(overrides)
    return state


# Every non-success return of PostProcessNode.execute(), by the fault that
# produces it. The point of parameterising is that a fix applied to one branch
# and not another is the commonest way this defect survives a review.
_ERROR_PATHS = {
    # (1) a pre-existing inner-workflow error: the branch that published
    #     error_log verbatim (scrubbed, which is not a closed set).
    "inner_workflow_error": (
        {"status": AgentStatus.ERROR.value},
        _REASON_WORKFLOW_FAILED,
    ),
    # (2)-(5) the output gate refusing to release the response.
    "gate_missing_record_evidence": (
        {"record_id": "", "record_ref": ""},
        _REASON_OUTPUT_WITHHELD,
    ),
    "gate_credential_in_narrative_field": (
        {"confirmation": _BEARER_LIKE},
        _REASON_OUTPUT_WITHHELD,
    ),
    "gate_credential_nested_in_payload": (
        {"rakus_payload": to_json({"expense_data": [{"items": [{"name": "Note", "values": [_BEARER_LIKE]}]}]})},
        _REASON_OUTPUT_WITHHELD,
    ),
    # A credential carried as a mapping KEY. A values-only walk reports zero
    # findings on exactly this case.
    "gate_credential_as_a_payload_key": (
        {"rakus_payload": to_json({_BEARER_LIKE: "memo"})},
        _REASON_OUTPUT_WITHHELD,
    ),
}


@pytest.mark.parametrize("overrides,expected_reason", _ERROR_PATHS.values(), ids=list(_ERROR_PATHS))
class TestEveryErrorPathPublishesClosedSetLabelsOnly:
    def test_envelope_values_are_all_declared_constants(self, overrides, expected_reason):
        """The envelope is {"reason": r}, r in ERROR_REASONS - on every path."""
        result = PostProcessNode().execute(_base_state(**overrides))
        assert result["status"] == AgentStatus.ERROR.value
        envelope = result["formatted_output"]
        assert set(envelope) == {"reason"}, f"envelope grew a key: {sorted(envelope)}"
        assert envelope["reason"] == expected_reason
        for _path, text in _strings(envelope):
            assert text in ERROR_REASONS | {"reason"}, f"envelope carries an undeclared value: {text!r}"

    def test_envelope_stays_truthy(self, overrides, expected_reason):
        """A falsy formatted_output re-opens `formatted_output or result`.

        AgentBaseGraph.get_output() applies no status check, so an empty
        envelope would hand the caller whatever survived in state instead.
        """
        result = PostProcessNode().execute(_base_state(**overrides))
        assert result["formatted_output"], "falsy envelope re-opens the `or result` fallback"

    def test_seeded_error_log_appears_nowhere_in_the_returned_mapping(self, overrides, expected_reason):
        """The whole delta, not just the envelope - nesting is not cover.

        error_log itself is exempt: it IS the internal channel, and the delta
        may add NEW entries to it (gate violations). What must not happen is
        the seeded upstream text coming back out through any other key.
        """
        result = PostProcessNode().execute(_base_state(**overrides))
        published = {k: v for k, v in result.items() if k != "error_log"}
        _assert_absent(_MARKER, published, "the returned node result")
        _assert_absent(_SENTINEL, published, "the returned node result")

    def test_the_answer_is_cleared_too(self, overrides, expected_reason):
        """Containment, not just a bounded error channel."""
        result = PostProcessNode().execute(_base_state(**overrides))
        assert result["result"] is None
        _assert_absent("Retrieved expense report", result, "the returned node result")

    def test_inner_error_log_entries_are_not_re_emitted(self, overrides, expected_reason):
        """The state reducer APPENDS, so re-emitting duplicates every line."""
        result = PostProcessNode().execute(_base_state(**overrides))
        assert _SENTINEL not in result.get("error_log", [])


class TestReasonsAreAClosedSet:
    def test_error_reasons_holds_exactly_the_two_declared_codes(self):
        assert ERROR_REASONS == frozenset({_REASON_WORKFLOW_FAILED, _REASON_OUTPUT_WITHHELD})

    def test_a_credential_shaped_key_is_withheld_from_the_violation_label(self):
        """The label rides error_log, where the framework's own scan would raise.

        FunctionNode._security_gate_output scans every value of a node result
        and RAISES on a credential finding - so a violation label that repeated
        a credential-shaped key would make this node's cleared result blow up
        and be replaced by a bare error, re-opening the `or result` fallback the
        clearing had just closed.
        """
        result = PostProcessNode().execute(_base_state(rakus_payload=to_json({_BEARER_LIKE: "memo"})))
        violations = result["error_log"]
        assert violations, "a credential carried as a KEY must still be found"
        assert any("<withheld>" in entry for entry in violations)
        assert not any("a" * 24 in entry for entry in violations)

    def test_a_framework_only_key_shape_is_withheld_too(self):
        """The local pattern is not the floor - the framework's detector is.

        `AKIA…` matches framework `detect_credentials` and NOT this module's
        own regex, so a label sanitized only by the local pattern would still
        make the framework's scan raise on the node result.
        """
        aws_like = "AKIA" + "B" * 16
        result = PostProcessNode().execute(
            _base_state(rakus_payload=to_json({aws_like: _BEARER_LIKE})),
        )
        joined = " ".join(result["error_log"])
        assert "<withheld>" in joined
        assert aws_like not in joined

    def test_a_clean_success_is_still_released(self):
        """Negative control: containment must not swallow a legitimate response."""
        result = PostProcessNode().execute(_base_state(error_log=[]))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"]["record_id"] == "7042"
        assert "reason" not in result["formatted_output"]


# ----------------------------------------------------------------------------
# End to end, through the deployed surface.
# ----------------------------------------------------------------------------

_TOKEN = "test-invoke-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_REQUEST = {
    "input": "Look up the expense report with report number 7042 and summarize the record on file.",
    "input_context": {"report_number": "7042"},
}


class TestErrorEnvelopeThroughTheRealInvoke:
    """Both non-success outcomes, driven through the real ASGI app.

    DATA-path faults in both cases: an inner node is made to hand the workflow
    a result with the upstream text in `error_log`. The gate is never touched -
    it refuses on its own terms (a SUCCESS response with no
    record_id/record_ref misrepresents the Rakus lookup), which is what puts
    the request on the contained path with the sentinel already in the
    internal channel.
    """

    @pytest.fixture()
    def client(self, monkeypatch):
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        import src.api.server as server

        return TestClient(server.app)

    @pytest.fixture()
    def refused_body(self, client, monkeypatch):
        """The GATE-VIOLATION path: confirm returns SUCCESS without record evidence."""

        def _confirm_without_evidence(self, state):
            return {
                "confirmation": "",
                "result": {"record_id": "", "confirmation": ""},
                "record_id": "",
                "record_ref": "",
                "error_log": [_SENTINEL],
                "status": AgentStatus.SUCCESS.value,
            }

        monkeypatch.setattr("src.nodes.confirm_node.ConfirmNode.execute", _confirm_without_evidence)
        response = client.post("/invoke", json=_REQUEST, headers=_AUTH)
        assert response.status_code == 200, response.text
        return response.json()

    @pytest.fixture()
    def inner_error_body(self, client, monkeypatch):
        """The INNER-ERROR path: the Rakus call fails with the sentinel logged."""

        def _call_fails(self, state, config=None):
            return {"status": AgentStatus.ERROR.value, "error_log": [_SENTINEL]}

        monkeypatch.setattr("src.nodes.call_rakus_api_node.CallRakusApiNode.execute", _call_fails)
        response = client.post("/invoke", json=_REQUEST, headers=_AUTH)
        assert response.status_code == 200, response.text
        return response.json()

    def test_a_clean_request_still_succeeds(self, client):
        """Positive control for the whole wiring: the fault-free path works."""
        body = client.post("/invoke", json=_REQUEST, headers=_AUTH).json()
        assert body["status"] == AgentStatus.SUCCESS.value, body
        assert body["output"]["record_id"] == "7042"
        assert "reason" not in body["output"]

    def test_the_fault_actually_reaches_the_output_gate(self, refused_body):
        """Positive control: without this the absence assertions prove nothing."""
        assert refused_body["status"] == AgentStatus.ERROR.value
        assert "PostProcessNode" in refused_body["node_history"], "the gate branch was never reached"

    def test_refused_invoke_publishes_the_reason_code_and_nothing_else(self, refused_body):
        assert refused_body["output"] == {"reason": _REASON_OUTPUT_WITHHELD}

    def test_seeded_error_log_appears_nowhere_in_the_refused_body(self, refused_body):
        """Walk the whole response - nested values AND nested keys."""
        _assert_absent(_MARKER, refused_body, "the /invoke response body")
        _assert_absent(_SENTINEL, refused_body, "the /invoke response body")
        # Belt and braces on the serialized form the caller actually receives.
        assert _MARKER not in json.dumps(refused_body, default=str)

    def test_no_node_authored_error_text_rides_the_refused_body(self, refused_body):
        """The gate's own violation sentences are node-authored too.

        They are bounded today, but they are not a closed set - which is the
        whole of molt's finding. They belong in error_log.
        """
        _assert_absent("PostProcess output gate", refused_body, "the /invoke response body")
        _assert_absent("record evidence", refused_body, "the /invoke response body")
        assert "error_log" not in refused_body

    def test_inner_error_invoke_publishes_no_error_text(self, inner_error_body):
        """post_process does not run on this path - the backbone routes to finalize.

        The envelope is therefore never built, and nothing of the failing
        node's words may appear in the body either.
        """
        assert inner_error_body["status"] == AgentStatus.ERROR.value
        assert "PostProcessNode" not in inner_error_body["node_history"]
        assert "error_log" not in inner_error_body
        _assert_absent(_MARKER, inner_error_body, "the /invoke response body")
        assert _MARKER not in json.dumps(inner_error_body, default=str)
