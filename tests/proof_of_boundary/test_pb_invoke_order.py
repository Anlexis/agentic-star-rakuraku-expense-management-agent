# PB-6: Backbone invoke-order + external-trust boundary (CMN-C2-280).
#
# A real outer-graph invoke exercising the fixed 5-node backbone
# (initialize -> pre_process -> main[inner Rakus workflow] -> post_process -> finalize).
#
# The SINGLE external trust gate lives on the outer backbone pre_process
# (required_trust_level = VERIFIED_EXTERNAL). GraphNode.execute() passes the
# caller's InvocationContext into the inner subgraph UNCHANGED (no trust
# elevation), so the inner Rakus call (CallRakusApiNode) is ANONYMOUS and
# runs under the caller's already-gated context. Two trust levels are asserted:
#
#   * VERIFIED_EXTERNAL (a real external caller - never for_internal()): passes
#     the pre_process gate, so the full backbone runs IN ORDER and the record
#     evidence surfaces in result["output"] (status success). The payload is
#     byte-equal to deploy/invoke_payload.json's "input" - the same request a
#     first invoke against a deployed instance sends.
#   * ANONYMOUS (an under-trusted caller): denied at the pre_process gate before
#     the inner Rakus call can run -> status=error, no record evidence.
#
# The Rakus call is served by the deterministic NETWORK-FREE stub transport
# (no live tenant, no secret needed - the node runs on the documented stub
# placeholder). Record evidence is asserted by presence (mask-robust: the
# framework/FinalizeNode may mask raw identifiers), never by whole-repr.

import importlib
import inspect
import json
import pathlib
import pkgutil
from typing import ClassVar

import pytest
from framework.nodes.base_node import BaseNode

try:
    from framework.schemas.agent_status import AgentStatus
    from framework.schemas.invocation_context import InvocationContext
    from framework.schemas.trust_level import TrustLevel

    from src.graph.graph import RakusExpenseAgent

    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - only when the framework wheel is absent
    _IMPORT_ERROR = exc

pytestmark = pytest.mark.skipif(_IMPORT_ERROR is not None, reason=f"framework wheel unavailable: {_IMPORT_ERROR}")

_MAIN_SLOT_NODE = "RakusWorkflowGraphNode"

# Byte-equal to deploy/invoke_payload.json "input" (asserted below - never retyped drift).
_VALID_PAYLOAD = "Look up the expense report with report number 7042 and summarize the record on file."
_SESSION_ID = "signoff-001"

_PAYLOAD_PATH = pathlib.Path(__file__).parents[2] / "deploy" / "invoke_payload.json"

_EXPECTED_HISTORY = [
    "InitializeNode",
    "PreProcessNode",
    _MAIN_SLOT_NODE,
    "PostProcessNode",
    "FinalizeNode",
]


def _build_agent():
    agent = RakusExpenseAgent()
    agent.compile()
    return agent


class TestBackboneInvokeOrder:
    def test_valid_payload_is_byte_equal_to_deploy_payload(self):
        """PB-6 uses the packaged deployment request verbatim."""
        deployed = json.loads(_PAYLOAD_PATH.read_text(encoding="utf-8"))
        assert deployed["input"] == _VALID_PAYLOAD
        assert deployed["session_id"] == _SESSION_ID

    def test_verified_external_runs_full_backbone_in_order(self):
        """External (VERIFIED_EXTERNAL) caller: the exact 5-node backbone runs
        in order and record evidence surfaces in result["output"]."""
        agent = _build_agent()
        result = agent.invoke(
            user_input=_VALID_PAYLOAD,
            session_id=_SESSION_ID,
            ctx=InvocationContext(caller_id="pb6-ext", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        )
        assert result["status"] == AgentStatus.SUCCESS.value, f"result={result!r}"
        assert result.get("trace_id") and result.get("correlation_id")
        assert result.get("node_history", []) == _EXPECTED_HISTORY
        # Record evidence + confirmation surface in result["output"] (presence,
        # mask-robust - never the raw identifier value).
        out = result.get("output") or {}
        assert out.get("record_id") or out.get("record_ref")
        assert out.get("confirmation")
        assert out.get("intent") == "lookup_expense"

    def test_under_trusted_caller_is_denied(self):
        """Under-trusted (ANONYMOUS) caller: ANONYMOUS < VERIFIED_EXTERNAL, so
        the pre_process gate refuses the request before the inner Rakus call
        can run - a well-formed error surface (gated, not crashed) with no
        record evidence. Note: the main slot NAME still appears in node_history
        because BaseNode.__call__ short-circuits on the already-errored state
        (its subgraph never executes); post_process is skipped by route()."""
        agent = _build_agent()
        result = agent.invoke(
            user_input=_VALID_PAYLOAD,
            session_id=_SESSION_ID,
            ctx=InvocationContext(caller_id="pb6-anon", caller_trust_level=TrustLevel.ANONYMOUS),
        )
        assert result["status"] == AgentStatus.ERROR.value, f"result={result!r}"
        assert "PostProcessNode" not in result.get("node_history", [])
        assert not result.get("output")

    def test_empty_input_surfaces_error_not_crash(self):
        agent = _build_agent()
        result = agent.invoke(
            user_input="   ",
            session_id=_SESSION_ID,
            ctx=InvocationContext(caller_id="pb6-ext", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        )
        assert result["status"] == AgentStatus.ERROR.value


# ---------------------------------------------------------------------------
# PB-6 (generic, per the scaffold's own bootstrap test): S-1 -> node_start ->
# S-2 -> execute() -> S-3 -> node_complete, verified for every concrete
# BaseNode subclass under src/nodes/. Scoped to src/nodes/ only - the outer
# GraphNode wrapper in src/graph/graph.py is covered above via a real
# agent.invoke() instead, since GraphNode.execute() rebuilds an
# InvocationContext that indexes session_id/thread_id/trace_id (not present in
# this generic fixture's minimal state) and is exercised only through a real
# compiled graph. All src/nodes/ leaf nodes were checked to degrade to a clean
# status=error (never raise) under a minimal state missing those fields, so no
# per-node fixture map is required here.
# ---------------------------------------------------------------------------


class _PrivilegedTrustGateFixture(BaseNode):
    """Always-present privileged node used to prove the S-1 negative boundary."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _security_gate_input(self, state):
        return state

    def execute(self, state):
        return {"status": "success"}

    def _security_gate_output(self, result):
        return result


def _trust_predecessor(required: TrustLevel) -> TrustLevel:
    """Return a lower valid trust level; fail loudly if the framework adds one."""
    predecessors = {
        TrustLevel.VERIFIED_EXTERNAL: TrustLevel.ANONYMOUS,
        TrustLevel.INTERNAL: TrustLevel.VERIFIED_EXTERNAL,
    }
    try:
        return predecessors[required]
    except KeyError as exc:
        raise AssertionError(f"no lower trust level defined for {required!r}") from exc


def _discover_node_classes() -> list[type]:
    """Import every module under src/nodes/ and collect concrete BaseNode subclasses."""
    try:
        pkg = importlib.import_module("src.nodes")
    except ImportError as exc:
        pytest.fail(f"PB-6 cannot import src.nodes; framework/template setup is broken: {exc}")

    discovered = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes."):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
            ):
                discovered.append(attr)
    return discovered


class TestInvokeOrder:
    """PB-6: __call__ must run S-1 -> node_start -> S-2 -> execute() -> S-3 -> node_complete."""

    def test_s1_denial_refuses_execution_before_execute(self, monkeypatch):
        """TC-08: an always-present privileged node proves the negative S-1 path."""
        import framework.nodes.base_node as base_node_module

        events: list[str] = []
        execute_calls: list[object] = []
        monkeypatch.setattr(
            base_node_module,
            "emit_trace_event",
            lambda event_type, _payload, _state: events.append(event_type),
        )
        original_execute = _PrivilegedTrustGateFixture.execute

        def spy_execute(self, state):
            execute_calls.append(state)
            return original_execute(self, state)

        monkeypatch.setattr(_PrivilegedTrustGateFixture, "execute", spy_execute)
        result = _PrivilegedTrustGateFixture()(
            {
                "caller_trust_level": _trust_predecessor(
                    _PrivilegedTrustGateFixture.required_trust_level
                ).value,
                "correlation_id": "tc08-s1-denial",
            }
        )

        assert result["status"] == "error"
        assert "S-1 trust gate denied" in result["error_log"][0]
        assert events == ["s1_denied"]
        assert not execute_calls

    def test_call_order_for_every_node(self, monkeypatch):
        node_classes = _discover_node_classes()
        if not node_classes:
            pytest.skip("no concrete BaseNode subclasses found under src/nodes/")

        import framework.nodes.base_node as base_node_module

        failures: list[str] = []
        for node_cls in node_classes:
            order: list[str] = []
            monkeypatch.setattr(
                base_node_module,
                "emit_trace_event",
                lambda event_type, _payload, _state, _o=order: _o.append(f"event:{event_type}"),
            )

            for method_name, label in (
                ("_security_gate_input", "security_gate_input"),
                ("execute", "execute"),
                ("_security_gate_output", "security_gate_output"),
            ):
                original = getattr(node_cls, method_name)

                def spy(self, arg, _o=order, _label=label, _orig=original):
                    _o.append(_label)
                    return _orig(self, arg)

                monkeypatch.setattr(node_cls, method_name, spy)

            instance = node_cls()
            # Minimal state PLUS the four lifecycle identity fields
            # (session_id/thread_id/trace_id alongside correlation_id):
            # CallRakusApiNode builds an InvocationContext via
            # InvocationContext.from_state(state), which INDEXES (not .get()s)
            # all four - a bare state raises KeyError before the gate-order
            # assertions below ever run (see the module-level SDK 1.0.3 note).
            state = {
                "caller_trust_level": node_cls.required_trust_level.value,
                "correlation_id": "pb6-invoke-order-test",
                "session_id": "pb6-invoke-order-test",
                "thread_id": "pb6-invoke-order-test",
                "trace_id": "pb6-invoke-order-test",
            }
            instance(state)

            expected = [
                "event:node_start",
                "security_gate_input",
                "execute",
                "security_gate_output",
                "event:node_complete",
            ]
            if order != expected:
                failures.append(
                    f"{node_cls.__name__}: invoke order violation.\n" f"expected: {expected}\nactual:   {order}"
                )

        assert not failures, "\n\n".join(failures)
