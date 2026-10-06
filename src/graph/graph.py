"""CMN-C2-280 outer graph (Cat 2).

Cat 2: fixed 5-node backbone (initialize -> pre_process -> main -> post_process ->
finalize). Domain complexity is encapsulated in RakusWorkflowGraphNode (`main`
slot), which wraps the inner RakusWorkflowGraph (validate -> classify -> infer ->
call -> confirm). add_edges() is NOT overridden - backbone wiring is the
framework's concern.
"""

from pathlib import Path
from typing import Any, ClassVar, TYPE_CHECKING

import yaml

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import State

if TYPE_CHECKING:
    from src.graph.domain_workflow_graph import RakusWorkflowGraph

# Repo root: src/graph/graph.py -> parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
# Runtime parameters live in config/config.yaml. config/agent.yaml is the STATIC
# manifest (flat registry schema) and carries no runtime block, so reading
# runtime values from it would always yield nothing.
_RUNTIME_CONFIG_PATH = _REPO_ROOT / "config" / "config.yaml"

# Runtime keys forwarded to the inner graph. `rakus` is the integration section;
# `timeout_s` is the per-call budget the Rakus client enforces. `max_retry` is
# read by the backbone itself from the graph config and is not forwarded here.
_FORWARDED_RUNTIME_KEYS = ("rakus", "timeout_s")


class RakusWorkflowGraphNode(GraphNode):
    """Wraps the inner Rakus expense workflow graph; assigned to the `main` slot.

    No constructor arguments (SDK v1 nodes are no-arg) - configuration reaches
    the subgraph via _parent_config(), which loads config/config.yaml.
    """

    # Fail fast: re-raise inner-graph exceptions as SubgraphError (default).
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def __init__(self) -> None:
        super().__init__()
        self._runtime_cache: dict[str, Any] | None = None

    def get_subgraph(self) -> "RakusWorkflowGraph":
        from src.graph.domain_workflow_graph import RakusWorkflowGraph

        return RakusWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        # pre_process serialized the request into validated_input (JSON string);
        # structured params travel as JSON and the first inner node parses them back.
        shaped: str = state.get("validated_input") or state.get("user_input", "")
        return shaped

    def merge_output(self, state: AgentState, sub_result: dict[str, Any]) -> dict[str, Any]:
        # Map only the keys this node changes back into the outer state.
        return {
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
            "intent": sub_result.get("intent", ""),
            "report_id": sub_result.get("report_id", ""),
            "record_id": sub_result.get("record_id", ""),
            "record_ref": sub_result.get("record_ref", ""),
            "expense_title": sub_result.get("expense_title", ""),
            "approval_status": sub_result.get("approval_status", ""),
            "confirmation": sub_result.get("confirmation", ""),
            "rakus_payload": sub_result.get("rakus_payload", ""),
            "redaction_flags": sub_result.get("redaction_flags", ""),
            "error_log": sub_result.get("error_log", []),
        }

    def _parent_config(self) -> dict[str, Any]:
        """Forward the runtime parameters to the inner graph under config["configurable"].

        Reads config/config.yaml - the live home of the runtime parameters - and
        forwards the `rakus` integration section and the `timeout_s` call budget.
        Returning an empty mapping here would make every declared runtime value
        dead, so the forwarded keys are asserted by the test suite end to end.
        """
        configurable: dict[str, Any] = {}
        for key in _FORWARDED_RUNTIME_KEYS:
            if key in self._runtime_config():
                configurable[key] = self._runtime_config()[key]
        return {"configurable": configurable}

    def _runtime_config(self) -> dict[str, Any]:
        """Load and cache config/config.yaml (runtime parameters)."""
        if self._runtime_cache is None:
            try:
                loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                # Missing/unreadable runtime config: inner nodes fall back to
                # their documented defaults rather than failing the invoke.
                loaded = None
            self._runtime_cache = loaded if isinstance(loaded, dict) else {}
        return self._runtime_cache


class RakusExpenseAgent(AgentBaseGraph):
    """CMN-C2-280 outer graph - Rakus Expense Agent.

    Backbone: initialize -> pre_process -> main -> post_process -> finalize (fixed).
    Domain logic lives in RakusWorkflowGraphNode (`main` slot); Rakus
    settings flow from config/config.yaml via _parent_config().
    """

    @property
    def name(self) -> str:
        return "cmn_c2_280"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = RakusWorkflowGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden - backbone wiring belongs to the framework.
