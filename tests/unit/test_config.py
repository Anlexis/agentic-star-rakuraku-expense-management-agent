# CMN-C2-280 - Unit tests: manifest + runtime-config sanity.
#
# The manifest (config/agent.yaml) is FLAT: the registry reads every key at the
# root, so there is no `agent:` block to nest under. Runtime parameters live in
# config/config.yaml and are a separate contract.

import pathlib

import pytest

try:
    import yaml  # pyyaml (transitive dep of the framework wheel)

    _YAML_ERROR = None
except Exception as exc:  # pragma: no cover
    yaml = None
    _YAML_ERROR = exc

_MANIFEST_PATH = pathlib.Path(__file__).parents[2] / "config" / "agent.yaml"
_RUNTIME_PATH = pathlib.Path(__file__).parents[2] / "config" / "config.yaml"

pytestmark = pytest.mark.skipif(_YAML_ERROR is not None, reason=f"pyyaml unavailable: {_YAML_ERROR}")


def _manifest():
    return yaml.safe_load(_MANIFEST_PATH.read_text())


def _runtime():
    return yaml.safe_load(_RUNTIME_PATH.read_text())


def test_manifest_identity():
    data = _manifest()
    assert data["id"] == "CMN-C2-280"
    assert data["category"] == "Cat 2"
    assert data["industry"] == "CMN"
    assert data["base_type"] == "ToolCallingAgent"


def test_manifest_entry_point():
    # Single dotted import path (module.Class), not split module/class keys.
    assert _manifest()["class"] == "src.graph.graph.RakusExpenseAgent"


def test_manifest_is_flat_no_nested_agent_block():
    # A nested `agent:` block would be invisible to the registry and would make
    # every value under it dead.
    assert "agent" not in _manifest()


def test_manifest_security():
    data = _manifest()
    # Agent-level entry trust, enforced by the outer backbone pre_process gate;
    # inner domain nodes stay ANONYMOUS.
    assert data["required_trust_level"] == "VERIFIED_EXTERNAL"


def test_manifest_declares_rakus_token_as_optional_not_required():
    # RAKUS_TOKEN is read with an OPTIONAL lookup, never a required one: the
    # default transport is network-free and runs without a credential.
    # Declaring it would make the agent fail to start wherever it is not
    # provisioned, so it must never appear in requires.secrets.
    assert "RAKUS_TOKEN" not in _manifest()["requires"]["secrets"]


def test_manifest_declares_azure_openai_secrets_and_extra():
    # ClassifyIntentNode's optional LLM enhancement resolves all three via
    # ctx.secrets.require(...) - declared here so the compile-time gate can
    # validate the names, even though a miss at runtime is caught by the
    # node's own except Exception (never fails the request).
    data = _manifest()
    assert set(data["requires"]["secrets"]) == {
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
    }
    assert data["requires"]["extras"] == ["openai"]


def test_runtime_config_rakus_integration_section():
    # Forwarded to the inner graph by RakusWorkflowGraphNode._parent_config().
    assert _runtime()["rakus"]["base_url"] == "https://api.rakurakuseisan.jp/api1"


def test_runtime_config_parameters():
    runtime = _runtime()
    assert isinstance(runtime["max_retry"], int)
    assert isinstance(runtime["timeout_s"], (int, float))
    # Every declared runtime key must have a live consumer; timeout_s reaching
    # the client is asserted end to end in test_domain_workflow_graph.py.
    assert runtime["timeout_s"] > 0
