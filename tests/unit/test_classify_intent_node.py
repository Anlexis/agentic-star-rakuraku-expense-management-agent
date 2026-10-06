# CMN-C2-280 - Unit tests: ClassifyIntentNode (inner Step 2)
# Intents: lookup_expense / register_expense / check_approval. Deterministic
# keyword heuristic is the BASELINE (always available); an optional Azure
# OpenAI call enhances it and overrides on a valid response, falling back to
# the keyword result on any failure - unknown (from either path) falls back
# to the read-only lookup. Keyword priority is check_approval >
# register_expense > lookup_expense: a status question must never trigger a
# write (docs/02_design.md DDR).
#
# Canon: invoked via node(state), so the full security pipeline runs (trust
# gate -> input gate -> execute -> output gate); inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.classify_intent_node import ClassifyIntentNode


class _FakeLLM:
    """Test-double for AzureOpenAIClient - same `complete(messages) -> dict` contract."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> dict[str, object]:
        self.calls.append(messages)
        return {"content": self._content}


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.classify_intent_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str) -> dict:
    return {
        "validated_input": text,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "classify-intent-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }


class TestClassifyIntentNode:
    def setup_method(self):
        self.node = ClassifyIntentNode()

    def test_keyword_lookup_expense(self):
        result = self.node(_state("Look up the expense report with report number 7042."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_expense"

    def test_keyword_register_expense(self):
        result = self.node(_state("Register a new expense for the taxi fare"))
        assert result["intent"] == "register_expense"

    def test_keyword_check_approval(self):
        result = self.node(_state("Has expense report 7042 been approved yet?"))
        assert result["intent"] == "check_approval"

    def test_approval_keyword_wins_over_register(self):
        # Priority order is check_approval-first: an approval-status question
        # about a submitted claim classifies as the read, never the write.
        result = self.node(_state("Check the approval status for the expense claim I submitted"))
        assert result["intent"] == "check_approval"

    def test_no_signal_defaults_to_readonly_lookup(self):
        result = self.node(_state("please handle this for the team"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_expense"
        # Non-fatal low-confidence note travels in error_log; status stays SUCCESS.
        assert any("defaulted to lookup_expense" in entry for entry in result.get("error_log", []))

    def test_empty_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_intent_label_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("Look up the expense report with report number 7042."))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - the label, never the text.
        assert payloads["classify_intent_complete"]["intent"] == "lookup_expense"
        assert payloads["classify_intent_complete"]["defaulted"] is False


class TestClassifyIntentNodeLLM:
    """Optional Azure OpenAI enhancement (test-double injection - no real Azure call)."""

    def test_llm_response_overrides_keyword_classification(self):
        """Valid LLM JSON overrides the keyword-derived intent."""
        # Keyword heuristic alone would say lookup_expense (no register/approval
        # signal); the LLM override picks check_approval instead.
        llm = _FakeLLM('{"intent": "check_approval"}')
        node = ClassifyIntentNode(llm=llm)
        result = node(_state("what about report 7042"))

        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "check_approval"
        assert len(llm.calls) == 1
        assert llm.calls[0][0]["role"] == "system"

    def test_llm_prose_wrapped_json_is_extracted(self):
        """LLM output wrapped in prose/markdown still parses (extract_json_object)."""
        llm = _FakeLLM('Sure, here you go:\n```json\n{"intent": "register_expense"}\n```')
        result = ClassifyIntentNode(llm=llm)(_state("please handle this for the team"))
        assert result["intent"] == "register_expense"

    def test_malformed_llm_response_falls_back_to_keyword(self):
        """Invalid shape (no `intent` key) -> keyword result kept, no crash."""
        llm = _FakeLLM('{"confidence": 0.9}')
        result = ClassifyIntentNode(llm=llm)(_state("Register a new expense for the taxi fare"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "register_expense"  # keyword result, unchanged

    def test_llm_invalid_intent_value_falls_back_to_keyword(self):
        """A syntactically valid but out-of-enum intent -> keyword result kept."""
        llm = _FakeLLM('{"intent": "delete_expense"}')
        result = ClassifyIntentNode(llm=llm)(_state("Register a new expense for the taxi fare"))
        assert result["intent"] == "register_expense"

    def test_llm_exception_falls_back_to_keyword(self):
        """LLM call raising (e.g. API error) -> keyword result kept, no crash."""

        class _RaisingLLM:
            def complete(self, messages):
                raise RuntimeError("simulated Azure OpenAI API error")

        result = ClassifyIntentNode(llm=_RaisingLLM())(_state("Look up the expense report with report number 7042."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_expense"

    def test_no_secret_configured_falls_back_to_keyword(self):
        """No llm= injected and no secret bound -> keyword result, no crash.

        This is the real production shape when register_nodes() never passes
        llm= and ctx.secrets.require("AZURE_OPENAI_API_KEY") has nothing bound
        (e.g. local/dev/CI with no Azure key configured).
        """
        result = ClassifyIntentNode()(_state("Register a new expense for the taxi fare"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "register_expense"

    def test_empty_input_skips_llm_call(self):
        """No validated_input -> _classify_via_llm never runs (error returned first)."""
        llm = _FakeLLM('{"intent": "register_expense"}')
        result = ClassifyIntentNode(llm=llm)(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert llm.calls == []

    def test_audit_emits_llm_inferred_flag(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        llm = _FakeLLM('{"intent": "check_approval"}')
        ClassifyIntentNode(llm=llm)(_state("what about report 7042"))
        payloads = {args[0]: args[1] for args in events}
        assert payloads["classify_intent_complete"]["llm_inferred"] is True
