"""Rakus (RakuRaku Seisan) expense API client.

Service layer: a thin wrapper around the Rakus expense SaaS REST API endpoints
(expense-report lookup, registration, approval-status check). Contains NO
business logic, NO routing, and NO credentials - the integration token is
passed in per call by the node (which reads it via ctx.secrets). This module
imports no framework internals - pure stdlib.

LIMITATION (deliberate, documented):
    The DEFAULT transport is a deterministic, NETWORK-FREE stub. It returns the
    documented Rakus response shapes (an ``expense_data`` list for lookups; an
    ``approval_status`` record for status checks; the task/receipt shape with a
    synthetic ``expense_number`` echo for registration, derived from the
    request) so the pipeline is runnable and testable without a live Rakus
    tenant or an HTTP library - it does NOT perform a live Rakus call. The
    limitation is documented rather than papered over with a faked call.

    To perform real Rakus calls, inject live transports (HTTP-backed ``post`` /
    ``get``) at construction time; the method contracts and payload shapes are
    documented per endpoint, so no business-logic change is needed to go live.
    Every transport receives the per-call ``timeout_s`` budget as its fourth
    argument, so a live adapter can pass it straight to its HTTP library. A
    live transport also requires a real integration token (see
    CallRakusApiNode - the stub runs without one because no request ever
    leaves the process).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

# A transport callable: (url, headers, json_body, timeout_s) -> (status_code, response_dict)
Transport = Callable[[str, "dict[str, str]", "dict[str, Any]", float], "tuple[int, dict[str, Any]]"]

_BASE_URL = "https://api.rakurakuseisan.jp/api1"
# Fallback call budget, used only when the runtime config declares none.
DEFAULT_TIMEOUT_S = 30.0
# Upper bound on a declared budget; a larger value is clamped rather than
# honoured, so a mis-declared config cannot hang a live call indefinitely.
MAX_TIMEOUT_S = 600.0


class RakusApiError(Exception):
    """Raised when the Rakus expense API returns a non-2xx status."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Rakus API error {status_code}: {message}")


class RakusClient:
    """Rakus (RakuRaku Seisan) expense-report client.

    Args:
        base_url: Rakus API base URL (default https://api.rakurakuseisan.jp/api1;
            tenant-specific in production - configured via config/config.yaml).
        post/get: optional injected transports (tests or a live client).
            When none is injected, a deterministic NETWORK-FREE stub is used
            (see the module docstring - it returns the documented shape without
            a live Rakus call).
        timeout_s: per-call budget handed to every transport. Non-finite,
            non-positive and non-numeric values fall back to DEFAULT_TIMEOUT_S;
            oversized values are clamped to MAX_TIMEOUT_S.
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        *,
        post: Transport | None = None,
        get: Transport | None = None,
        timeout_s: float | int | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._post = post
        self._get = get
        self.timeout_s = _sane_timeout(timeout_s)

    # -- transport mode --------------------------------------------------------

    @property
    def uses_stub_transport(self) -> bool:
        """True when NO live transport is injected (the network-free v1 default)."""
        return self._post is None and self._get is None

    # -- auth ----------------------------------------------------------------

    def _headers(self, api_token: str) -> dict[str, str]:
        """Build the Rakus API auth headers.

        api_token is supplied per-call by the node (from ctx.secrets); it is
        never persisted on the instance or logged.
        """
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_token}",
        }

    # -- v1 deterministic stub transport (default; NO network) ----------------

    def _stub_transport(
        self, url: str, headers: dict[str, str], json_body: dict[str, Any], timeout_s: float
    ) -> tuple[int, dict[str, Any]]:
        """Deterministic, network-free stub - returns the documented Rakus shape.

        NOT a live call, so the timeout budget is accepted (to honour the
        transport contract) and unused. Synthetic ids are derived from the
        request so the response is stable and inspectable. See the module
        docstring for the limitation and how to inject live transports.
        """
        seed = url + "|" + json.dumps(json_body, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        op = json_body.get("_rakus_op")
        if op == "lookup":
            number = str(json_body.get("report_number", "")) or f"e-{digest[:8]}"
            # Documented GET /expenses shape: {"updated_at": ..., "expense_data": [...]}
            return 200, {
                "updated_at": "1970-01-01 00:00:00",
                "expense_data": [
                    {
                        "number": number,
                        "title": f"Expense report {number}",
                        "status": "submitted",
                        "items": [],
                    }
                ],
                "_stub": True,  # marks the network-free stub response
            }
        if op == "status":
            number = str(json_body.get("report_number", "")) or f"e-{digest[:8]}"
            # Documented approval-status shape for a single report.
            return 200, {
                "expense_number": number,
                "approval_status": "pending",
                "_stub": True,  # marks the network-free stub response
            }
        # POST /expenses (register) - documented task-receipt shape, plus a
        # synthetic expense_number echo so the caller can reference the affected
        # record without a follow-up lookup.
        expense_data = json_body.get("expense_data") or [{}]
        first = expense_data[0] if isinstance(expense_data, list) and expense_data else {}
        number = str(first.get("number", "")) or f"e-{digest[:8]}"
        return 200, {
            "task_id": int(digest[:6], 16),
            "expense_number": number,
            "_stub": True,  # marks the network-free stub response
        }

    def _resolve(self, injected: Transport | None) -> Transport:
        return injected or self._stub_transport

    # -- public API ---------------------------------------------------------

    def find_expense(self, report_number: str, api_token: str) -> dict[str, Any]:
        """GET /expenses - look up an expense report by report number.

        The live Rakus endpoint returns the report list; a live ``get``
        transport adapter is expected to filter to ``report_number`` (the stub
        returns the matching record directly). Returns the parsed response dict
        (containing ``expense_data``). Raises RakusApiError on non-2xx.
        """
        url = f"{self._base_url}/expenses"
        transport = self._resolve(self._get)
        status, body = transport(
            url,
            self._headers(api_token),
            {"_rakus_op": "lookup", "report_number": report_number},
            self.timeout_s,
        )
        if not (200 <= status < 300):
            raise RakusApiError(status, _err_message(body))
        return body

    def create_expense(self, payload: dict[str, Any], api_token: str) -> dict[str, Any]:
        """POST /expenses - register a new expense report.

        ``payload`` is the documented ``{"expense_data": [...]}`` request body.
        Returns the parsed response dict (task receipt). Raises RakusApiError
        on a non-2xx status.
        """
        url = f"{self._base_url}/expenses"
        transport = self._resolve(self._post)
        status, body = transport(url, self._headers(api_token), payload, self.timeout_s)
        if not (200 <= status < 300):
            raise RakusApiError(status, _err_message(body))
        return body

    def get_approval_status(self, report_number: str, api_token: str) -> dict[str, Any]:
        """GET /expenses/status - check the approval workflow status of a report.

        Returns the parsed response dict (containing ``approval_status`` and
        the ``expense_number`` echo). Raises RakusApiError on a non-2xx status.
        """
        url = f"{self._base_url}/expenses/status"
        transport = self._resolve(self._get)
        status, body = transport(
            url,
            self._headers(api_token),
            {"_rakus_op": "status", "report_number": report_number},
            self.timeout_s,
        )
        if not (200 <= status < 300):
            raise RakusApiError(status, _err_message(body))
        return body


def _sane_timeout(value: float | int | None) -> float:
    """Coerce a declared budget to a finite, positive, bounded number of seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_TIMEOUT_S
    timeout = float(value)
    # NaN fails every comparison, so test it explicitly rather than relying on
    # the range check below to reject it.
    if timeout != timeout or timeout in (float("inf"), float("-inf")):
        return DEFAULT_TIMEOUT_S
    if timeout <= 0:
        return DEFAULT_TIMEOUT_S
    return min(timeout, MAX_TIMEOUT_S)


def _err_message(body: Any) -> str:
    """Extract a human-readable error message from a Rakus error body."""
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(e) for e in errors)
        msg = body.get("message")
        if msg:
            return str(msg)
    return str(body)
