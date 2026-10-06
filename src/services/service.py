"""Domain service layer."""

from __future__ import annotations

from typing import Any


class Service:
    """Domain service."""

    async def fetch(self, query: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch domain data for the given query."""
        raise NotImplementedError("Implement fetch() for Service")
