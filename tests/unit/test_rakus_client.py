# CMN-C2-280 - Unit tests: RakusClient service (Rakus RakuRaku Seisan API shape)
# Pure service layer (stdlib-only, no framework imports) - plain function tests.
#
# Every transport receives (url, headers, body, timeout_s); the fourth argument
# is the per-call budget a live adapter hands to its HTTP library.

import pytest

from src.services.rakus_client import (
    DEFAULT_TIMEOUT_S,
    MAX_TIMEOUT_S,
    RakusApiError,
    RakusClient,
)


def test_find_expense_success_with_injected_get():
    captured = {}

    def get(url, headers, body, timeout_s):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        captured["timeout_s"] = timeout_s
        return 200, {
            "updated_at": "2026-01-01 00:00:00",
            "expense_data": [{"number": "7042", "title": "Expense report 7042"}],
        }

    client = RakusClient("https://rakus.example.test/api1/", get=get, timeout_s=12)
    resp = client.find_expense("7042", "tok123")
    assert resp["expense_data"][0]["number"] == "7042"
    # Trailing slash stripped from base_url; documented /expenses endpoint.
    assert captured["url"] == "https://rakus.example.test/api1/expenses"
    # Rakus API auth: the per-call token travels as a Bearer Authorization header.
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["body"]["report_number"] == "7042"
    # The declared call budget reaches the transport.
    assert captured["timeout_s"] == 12.0


def test_create_expense_success_with_injected_post():
    captured = {}

    def post(url, headers, body, timeout_s):
        captured["url"] = url
        captured["body"] = body
        captured["timeout_s"] = timeout_s
        return 200, {"task_id": 42, "expense_number": "9002"}

    client = RakusClient("https://rakus.example.test/api1", post=post)
    payload = {"expense_data": [{"number": "9002", "title": "Taxi"}]}
    resp = client.create_expense(payload, "tok")
    assert resp["expense_number"] == "9002"
    assert captured["url"] == "https://rakus.example.test/api1/expenses"
    assert captured["body"] == payload


def test_get_approval_status_success_with_injected_get():
    captured = {}

    def get(url, headers, body, timeout_s):
        captured["url"] = url
        captured["body"] = body
        return 200, {"expense_number": "7042", "approval_status": "approved"}

    client = RakusClient("https://rakus.example.test/api1", get=get)
    resp = client.get_approval_status("7042", "tok")
    assert resp["approval_status"] == "approved"
    assert captured["url"] == "https://rakus.example.test/api1/expenses/status"
    assert captured["body"]["report_number"] == "7042"


def test_non_2xx_raises_rakus_api_error():
    def post(url, headers, body, timeout_s):
        return 400, {"errors": ["expense_data is malformed"]}

    client = RakusClient("https://rakus.example.test/api1", post=post)
    with pytest.raises(RakusApiError) as exc:
        client.create_expense({"expense_data": [{}]}, "tok")
    assert exc.value.status_code == 400
    assert "expense_data is malformed" in str(exc.value)


def test_default_stub_transport_lookup_shape():
    # No transport injected -> deterministic, network-free stub.
    client = RakusClient()
    assert client.uses_stub_transport is True
    resp = client.find_expense("7042", "tok")
    assert resp.get("_stub") is True
    record = resp["expense_data"][0]
    assert record["number"] == "7042"
    assert record["title"] == "Expense report 7042"


def test_default_stub_transport_status_shape():
    client = RakusClient()
    resp = client.get_approval_status("7042", "tok")
    assert resp.get("_stub") is True
    assert resp["expense_number"] == "7042"
    assert resp["approval_status"] == "pending"


def test_default_stub_transport_create_echoes_expense_number():
    client = RakusClient()
    resp = client.create_expense({"expense_data": [{"number": "9002", "title": "Taxi"}]}, "tok")
    assert resp.get("_stub") is True
    assert resp["expense_number"] == "9002"
    assert isinstance(resp["task_id"], int)


def test_injected_transport_disables_stub_flag():
    client = RakusClient(get=lambda url, headers, body, timeout_s: (200, {"expense_data": []}))
    assert client.uses_stub_transport is False


# -- call budget ------------------------------------------------------------


def test_timeout_defaults_when_undeclared():
    assert RakusClient().timeout_s == DEFAULT_TIMEOUT_S


@pytest.mark.parametrize(
    "declared",
    [None, "30", float("nan"), float("inf"), float("-inf"), 0, -5, True],
)
def test_non_finite_or_unusable_timeout_falls_back(declared):
    # A non-finite budget compares False against every bound, so an unchecked
    # value would silently disable the deadline it exists to enforce.
    assert RakusClient(timeout_s=declared).timeout_s == DEFAULT_TIMEOUT_S


def test_oversized_timeout_is_clamped():
    assert RakusClient(timeout_s=10_000).timeout_s == MAX_TIMEOUT_S
