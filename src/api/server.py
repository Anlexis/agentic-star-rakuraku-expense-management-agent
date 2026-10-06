"""Standalone HTTP entry point for the agent.

Entry points are adapters only - no business logic here. The adapter's job is
to establish caller trust, bound the request envelope, and hand the request to
the graph; every domain decision belongs to the nodes.
"""

import os
import secrets
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import RakusExpenseAgent

app = FastAPI(title="Agent")

agent = RakusExpenseAgent()
agent.compile()
agent.provision_secrets(secrets_factory(namespace="cmn-c2-280", agent_name="RakusExpenseAgent"))

# Envelope bounds enforced before the request reaches the graph. Field-level
# validation (shape, alphabet, ranges) belongs to the node that owns the caller
# contract; these caps only stop an oversized body from being parsed at all.
MAX_INPUT_CHARS = 8000
MAX_CONTEXT_KEYS = 32


class InvokeRequest(BaseModel):
    """Request envelope for POST /invoke."""

    input: str = Field(max_length=MAX_INPUT_CHARS)
    session_id: str = Field(default="", max_length=128)
    # Structured data supplied alongside the request text (expense report
    # reference, expense fields). Validated by the pre_process node.
    input_context: dict[str, Any] = Field(default_factory=dict)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> dict[str, Any]:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone caller auth: when INVOKE_AUTH_TOKEN is set, callers still
    # ANONYMOUS must present it as a Bearer token and run at VERIFIED_EXTERNAL.
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
    if len(req.input_context) > MAX_CONTEXT_KEYS:
        # Names the limit, never the submitted keys.
        raise HTTPException(status_code=400, detail="Request context has too many entries.")
    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        response: dict[str, Any] = agent.invoke(req.input, ctx=ctx, input_context=req.input_context)
        return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "RakusExpenseAgent"}
