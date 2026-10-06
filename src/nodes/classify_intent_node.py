"""Inner workflow Step 2: ClassifyIntent.

Classifies the (redacted) request into one of lookup_expense / register_expense
/ check_approval. The deterministic keyword heuristic below is the BASELINE -
always available, fully reproducible, and what runs when no LLM is configured
- so the step remains testable and runnable without a language model. An
optional LLM (Azure OpenAI) call enhances this for phrasing the keyword list
misses; it OVERRIDES the heuristic result only when it succeeds and returns a
valid intent, and any failure (missing secret, API error, malformed response)
silently keeps the heuristic classification - this step must always produce
an intent, never hard-fail the pipeline over an LLM outage. Low-confidence /
unknown (from either path) falls back to the read-only "lookup_expense"
default with a note - never a write.

Keyword priority is check_approval > register_expense > lookup_expense:
approval-status wording ("approval", "status", "承認") frequently co-occurs
with registration verbs ("申請"), and a status question must never trigger a
write; a pure registration request carries no approval keyword, so the
ordering costs nothing.
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.services.llm.azure_openai_client import AzureOpenAIClient
from shared.utils.audit_logger import emit_trace_event
from shared.utils.llm_json import extract_json_object

_VALID_INTENTS = ("lookup_expense", "register_expense", "check_approval")

_SYSTEM_PROMPT = (
    "You are an expense-workflow intent classifier. Given a redacted expense "
    "request, classify it into exactly one of: lookup_expense (look up / show "
    "/ summarize an existing report), register_expense (submit / file / "
    "record a new expense), check_approval (check the approval / status of a "
    "report). A status question is never register_expense even if it "
    "mentions submission. Respond with a single JSON object only, no prose, "
    'no markdown fences: {"intent": "lookup_expense"|"register_expense"|'
    '"check_approval"}.'
)

# Deterministic keyword signals (checked in priority order - see module docstring).
_KEYWORDS = (
    (
        "check_approval",
        ("approval", "approved", "approve", "status", "pending", "承認", "状況", "ステータス", "進捗", "決裁"),
    ),
    (
        "register_expense",
        (
            "register",
            "submit",
            "file an expense",
            "file a new",
            "claim",
            "reimburse",
            "record an expense",
            "new expense",
            "add an expense",
            "expense entry for",
            "登録",
            "申請",
            "起票",
            "作成",
            "新規",
            "追加",
            "立替",
        ),
    ),
    (
        "lookup_expense",
        (
            "look up",
            "lookup",
            "find",
            "show",
            "get",
            "fetch",
            "retrieve",
            "search",
            "list",
            "summarize",
            "what is",
            "report for",
            "report of",
            "照会",
            "検索",
            "参照",
            "確認",
            "一覧",
            "教えて",
        ),
    ),
)


class ClassifyIntentNode(FunctionNode):
    """Classify the request into a Rakus expense operation intent."""

    # Inner domain node, read-only classification of already-redacted text -
    # the external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def __init__(self, llm: Any | None = None) -> None:
        super().__init__()
        # `llm` is a test-double seam only - register_nodes() never passes one
        # in production. The real client is built fresh per invocation inside
        # execute() (from ctx.secrets: api_key + azure_endpoint +
        # azure_deployment, all three declared secrets, none of it in
        # config/config.yaml), never cached on self: node instances are
        # constructed once in register_nodes() (registry LRU cache, shared
        # across every invocation) before any request's secrets are
        # provisioned, and caching one caller's client would leave it visible
        # to the next caller.
        self._llm = llm

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        text = state.get("validated_input", "") or ""
        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: missing validated_input"],
            }

        intent = self._classify_via_keywords(text)
        llm_intent = self._classify_via_llm(text, state)
        defaulted_to_keywords = llm_intent is None
        if llm_intent is not None:
            intent = llm_intent

        note: list[str] = []
        if intent not in _VALID_INTENTS:
            note = ["ClassifyIntentNode: low-confidence classification, " "defaulted to lookup_expense (read-only)"]
            intent = "lookup_expense"

        # Audit the classification decision - intent label only, never the text.
        emit_trace_event(
            "classify_intent_complete",
            {"intent": intent, "defaulted": bool(note), "llm_inferred": not defaulted_to_keywords},
            state,
        )

        result: dict[str, Any] = {"intent": intent, "status": AgentStatus.SUCCESS.value}
        if note:
            result["error_log"] = note  # non-fatal note; status stays SUCCESS
        return result

    # -- classification -------------------------------------------------------

    def _classify_via_keywords(self, text: str) -> str:
        low = text.lower()
        for intent, words in _KEYWORDS:
            if any(w in low for w in words):
                return intent
        # No signal at all: fall through to the read-only default via the
        # _VALID_INTENTS guard in execute() (returns a sentinel outside the set).
        return "unknown"

    def _classify_via_llm(self, text: str, state: dict[str, Any]) -> "str | None":
        """LLM-based intent classification; overrides the keyword heuristic.

        Returns None on any failure (missing secret, API error, malformed
        response) or an intent outside _VALID_INTENTS - caller keeps the
        keyword classification already computed. Never raises.
        """
        try:
            llm = self._llm
            if llm is None:
                ctx = InvocationContext.from_state(state)
                llm = AzureOpenAIClient(
                    {
                        "api_key": ctx.secrets.require("AZURE_OPENAI_API_KEY"),
                        "azure_endpoint": ctx.secrets.require("AZURE_OPENAI_ENDPOINT"),
                        "azure_deployment": ctx.secrets.require("AZURE_OPENAI_DEPLOYMENT"),
                    }
                )
            response = llm.complete(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ]
            )
            parsed = extract_json_object(response.get("content", ""))
            intent = parsed.get("intent") if isinstance(parsed, dict) else None
            return intent if intent in _VALID_INTENTS else None
        except Exception:
            return None
