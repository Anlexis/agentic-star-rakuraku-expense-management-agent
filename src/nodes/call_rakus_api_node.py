"""Inner workflow Step 4: CallRakusApi (tool side-effect).

Performs the lookup/register/approval-status call against the Rakus (RakuRaku
Seisan) expense API endpoints via src/services/rakus_client.py.

Security posture:
  Trust: required_trust_level = ANONYMOUS. The single external trust gate lives
       on the OUTER backbone pre_process (VERIFIED_EXTERNAL), not on this inner
       node. GraphNode.execute() passes the caller's InvocationContext into the
       inner subgraph UNCHANGED (no trust elevation), so a real external caller
       runs this call under its own VERIFIED_EXTERNAL context; declaring
       INTERNAL here would deny that already-gated external caller before the
       call ever runs. The node therefore stays ANONYMOUS.
  Credentials: the integration token is read via
       ctx.secrets.get("RAKUS_TOKEN") (InvocationContext.from_state(state)) -
       never os.environ, never stored in state. While the deterministic
       NETWORK-FREE stub transport is active a missing token is tolerated (a
       sentinel placeholder is used - it is never sent anywhere because no
       request leaves the process); with a LIVE transport injected, a missing
       token is a hard status=error - a real API is never called
       unauthenticated.
  Audit: emit_trace_event() is called on the success path - a side-effect
       against an external expense system; HTTP 4xx/5xx surfaces as
       status=error + error_log (no silent pass).
  Error reasons: CLOSED-SET labels only - what was not found, the HTTP status,
       the exception class - never the report number, the expense content, or
       an upstream Rakus response body (unbounded third-party text that can
       quote the very record it refused). `error_log` is the INTERNAL channel
       (post_process publishes a constant reason code and never projects it)
       and it still has to be closed-set: the audit trail reads it, a
       checkpoint keeps it, and the framework's own egress scan raises on
       credential-shaped text anywhere in a node result - which would replace a
       contained result with a bare error.

Configuration: this node takes NO constructor arguments (SDK v1 nodes are
no-arg). Rakus settings arrive as state fields - `rakus_config` (base_url) and
`rakus_timeout_s` (per-call budget), injected by the inner graph's
_extra_initial_state() from the runtime config forwarded by
RakusWorkflowGraphNode._parent_config() - or via the optional
`config["configurable"]["rakus"]` argument for direct invocation. The client is
constructed locally per call (no module-global mutation).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json
from src.services.rakus_client import RakusApiError, RakusClient

_SECRET_KEY = "RAKUS_TOKEN"
# Placeholder handed to the network-free stub transport when no secret is
# provisioned. Never sent over any network (the stub performs no I/O) and never
# written to state or logs.
_STUB_PLACEHOLDER = "stub-transport-no-credential"

# Fixed reasons for the failures whose natural message would otherwise be
# written by something outside this module - a caught exception's text, or a
# value read out of state. error_log is the audit channel the state reducer
# accumulates and a checkpoint keeps, so what goes into it has to be this
# module's own words.
_REASON_NO_REPORT_FOUND = "CallRakusApiNode: no expense report found for the requested number"
_REASON_NO_APPROVAL_STATUS = "CallRakusApiNode: no approval status returned for the requested number"
_REASON_UNKNOWN_INTENT = "CallRakusApiNode: unknown intent - refusing an unrecognised operation"


class CallRakusApiNode(FunctionNode):
    """Look up / register / check approval of a Rakus expense report."""

    # The external trust gate is enforced UPSTREAM on the outer backbone
    # pre_process (VERIFIED_EXTERNAL). This inner node runs under the caller's
    # UNELEVATED context (GraphNode does not elevate trust for the subgraph), so
    # it must stay ANONYMOUS - declaring INTERNAL would deny a real external
    # caller before the call runs.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = from_json(state.get("rakus_payload"), None)
        if not payload:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallRakusApiNode: missing rakus_payload"],
            }

        intent = state.get("intent", "lookup_expense") or "lookup_expense"

        # Settings from state (graph-injected), overridable via an explicit
        # config["configurable"]["rakus"] for direct invocation. Merged into a
        # LOCAL dict - module globals are never mutated.
        settings = dict(from_json(state.get("rakus_config"), {}) or {})
        configurable = (config or {}).get("configurable") or {}
        settings.update(configurable.get("rakus") or {})
        timeout_s = configurable.get("timeout_s", state.get("rakus_timeout_s"))

        # Client built locally per call; with no injected transport it uses the
        # deterministic NETWORK-FREE stub (documented limitation, docs/02).
        base_url = str(settings.get("base_url", "") or "").strip()
        client = RakusClient(base_url=base_url, timeout_s=timeout_s) if base_url else RakusClient(timeout_s=timeout_s)

        # Token from the bound secret provider - never os.environ / state.
        ctx = InvocationContext.from_state(state)
        api_token = ctx.secrets.get(_SECRET_KEY)
        if api_token is None:
            if client.uses_stub_transport:
                # Stub limitation: no request leaves the process, so run with
                # a non-credential placeholder (see module docstring).
                api_token = _STUB_PLACEHOLDER
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        f"CallRakusApiNode: secret {_SECRET_KEY} unavailable - "
                        "refusing to call a live transport unauthenticated"
                    ],
                }

        report_id = state.get("report_id", "") or str(payload.get("report_number", "") or "")
        approval_status = ""

        try:
            if intent == "lookup_expense":
                if not report_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallRakusApiNode: unresolved report number - cannot look up expense report"],
                    }
                resp = client.find_expense(report_id, api_token) or {}
                reports = resp.get("expense_data") or []
                if not reports:
                    # Names WHAT was not found, never WHICH record: the report
                    # number is caller-derived, and a log line that carried it
                    # would put the record evidence into the audit trail and
                    # any checkpoint reader.
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": [_REASON_NO_REPORT_FOUND],
                    }
                record = reports[0]
                record_id = str(record.get("number", "")) or report_id
                expense_title = state.get("expense_title", "") or str(record.get("title", ""))
            elif intent == "check_approval":
                if not report_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallRakusApiNode: unresolved report number - cannot check approval status"],
                    }
                resp = client.get_approval_status(report_id, api_token) or {}
                approval_status = str(resp.get("approval_status", "") or "")
                if not approval_status:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": [_REASON_NO_APPROVAL_STATUS],
                    }
                record_id = str(resp.get("expense_number", "")) or report_id
                expense_title = state.get("expense_title", "")
            elif intent == "register_expense":
                resp = client.create_expense(payload, api_token) or {}
                record_id = str(resp.get("expense_number", "")) or report_id
                expense_title = state.get("expense_title", "")
            else:
                # The intent VALUE is not echoed: it is read out of state, so
                # it is not a label this module chose, and this branch is only
                # reachable when it is something the classifier never produces.
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [_REASON_UNKNOWN_INTENT],
                }
        except RakusApiError as exc:
            # HTTP status only. A live tenant's error body is unbounded
            # third-party text that can echo the very record it refused, and
            # RakusApiError's own message embeds that body - so the status is
            # bound to a LOCAL first and the exception itself never appears in
            # the f-string. Interpolating the exception is exactly how an
            # upstream response body reaches error_log.
            http_status = exc.status_code
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallRakusApiNode: Rakus API error {http_status}"],
            }
        except Exception as exc:  # transport failure - no silent pass
            # Exception CLASS only, bound to a local for the same reason: a
            # transport error message routinely carries hostnames, paths and
            # request detail that must not ride out.
            failure_kind = type(exc).__name__
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallRakusApiNode: Rakus call failed ({failure_kind})"],
            }

        record_ref = f"rakus://expenses/{record_id}" if record_id else ""

        # Audit the tool side-effect - intent + presence signals only, never
        # expense content or credentials.
        emit_trace_event(
            "call_rakus_api_complete",
            {
                "intent": intent,
                "has_record_id": bool(record_id),
                "stub_transport": client.uses_stub_transport,
                "timeout_s": client.timeout_s,
            },
            state,
        )

        return {
            "record_id": record_id,
            "record_ref": record_ref,
            "report_id": report_id or record_id,
            "expense_title": expense_title,
            "approval_status": approval_status,
            "status": AgentStatus.SUCCESS.value,
        }
