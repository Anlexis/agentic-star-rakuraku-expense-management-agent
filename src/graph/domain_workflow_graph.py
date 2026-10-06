"""Inner Rakus expense workflow graph (Cat 2 domain workflow).

Instantiated by RakusWorkflowGraphNode.get_subgraph() in graph.py. Inherits
BaseGraph directly for a fully custom linear topology:

    START -> validate_input -> classify_intent -> infer_rakus_fields
          -> call_rakus_api -> confirm -> END

Config (forwarded from the outer graph via _parent_config(), under
config["configurable"], sourced from config/config.yaml):
    rakus      - integration section (base_url, ...); injected into State as
                 the JSON `rakus_config` field via _extra_initial_state() so
                 the no-arg nodes can read it
    timeout_s  - per-call Rakus budget; injected into State as `rakus_timeout_s`
                 and enforced by the client on every call

Nodes are registered WITHOUT constructor arguments (SDK v1 nodes are no-arg;
ctor args raise TypeError at graph build).
"""

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from src.nodes.validate_input_node import ValidateInputNode
from src.nodes.classify_intent_node import ClassifyIntentNode
from src.nodes.infer_rakus_fields_node import InferRakusFieldsNode
from src.nodes.call_rakus_api_node import CallRakusApiNode
from src.nodes.confirm_node import ConfirmNode
from src.schemas.state import State, to_json

# A declared call budget must be a finite, positive, sanely bounded number;
# anything else is ignored so the client falls back to its documented default
# rather than running with a nonsense (or non-finite) deadline.
_MAX_TIMEOUT_S = 600.0


def _coerce_timeout(value: object) -> float | None:
    """Return a finite, positive, in-range timeout, or None when unusable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    timeout = float(value)
    if not (timeout == timeout) or timeout in (float("inf"), float("-inf")):
        return None
    if not (0 < timeout <= _MAX_TIMEOUT_S):
        return None
    return timeout


class RakusWorkflowGraph(BaseGraph):
    """Inner graph: NL -> validate -> classify -> infer -> call -> confirm."""

    @property
    def name(self) -> str:
        return "rakus_expense_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory config: the rakus section is optional (the client
        # falls back to the documented default base_url + the network-free
        # stub transport), and a missing/unusable setting is handled at
        # CallRakusApiNode.execute() as a graceful status=error rather than
        # a compile-time crash.
        pass

    def register_nodes(self) -> None:
        # No super() - BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize / finalize (outer backbone concern). All nodes are no-arg.
        self._nodes["validate_input"] = ValidateInputNode()
        self._nodes["classify_intent"] = ClassifyIntentNode()
        self._nodes["infer_rakus_fields"] = InferRakusFieldsNode()
        self._nodes["call_rakus_api"] = CallRakusApiNode()
        self._nodes["confirm"] = ConfirmNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "validate_input")
        self._sg.add_edge("validate_input", "classify_intent")
        self._sg.add_edge("classify_intent", "infer_rakus_fields")
        self._sg.add_edge("infer_rakus_fields", "call_rakus_api")
        self._sg.add_edge("call_rakus_api", "confirm")
        self._sg.add_edge("confirm", END)

    def route(self, state: State) -> str:
        # Required by BaseGraph ABC. Annotated with this graph's OWN State:
        # LangGraph reads a path callable's annotation as its input schema and
        # projects away every field the annotation does not declare, so a
        # broader base annotation would hide domain fields from any future
        # conditional edge. The topology is linear today, so nothing calls this.
        return END if state.get("status") == AgentStatus.ERROR.value else "confirm"

    def _extra_initial_state(self) -> dict[str, Any]:
        # Seed the runtime parameters (arriving under config["configurable"]
        # from _parent_config()) into State so the no-arg nodes can read them.
        # Container values are stored as JSON strings to stay checkpoint-safe.
        configurable = self.config.get("configurable") or {}
        extra: dict[str, Any] = {}
        rakus = configurable.get("rakus") or {}
        if rakus:
            extra["rakus_config"] = to_json(rakus)
        timeout_s = _coerce_timeout(configurable.get("timeout_s"))
        if timeout_s is not None:
            extra["rakus_timeout_s"] = timeout_s
        return extra

    def get_output(self, state: State) -> dict[str, Any]:
        return {
            "output": state.get("result") or state.get("confirmation"),
            "status": state.get("status"),
            "intent": state.get("intent", ""),
            "report_id": state.get("report_id", ""),
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "expense_title": state.get("expense_title", ""),
            "approval_status": state.get("approval_status", ""),
            "confirmation": state.get("confirmation", ""),
            "rakus_payload": state.get("rakus_payload", ""),
            "redaction_flags": state.get("redaction_flags", ""),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
            "node_history": state.get("node_history", []),
        }
