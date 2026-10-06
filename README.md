# Rakuraku Expense Management Agent

AI agent for managing expense claims in Rakus Rakuraku Seisan, built with Agentic Star.

> **Category**: Cat 2 (multi-step domain workflow)
> **Industry**: Common
> **Template ID**: CMN-C2-280

## Overview

Turns a plain-language expense request into an action against the Rakus
(RakuRaku Seisan) expense API. It classifies what the request is asking for -
look up a report, register a new one, or check an approval status - pulls the
report number and expense fields out of the request, calls the matching API
endpoint, and returns a confirmation naming the record it touched.

Two design choices are worth knowing before you adapt it. The report number is
never guessed: it is taken only from an explicit number in the request or from
the structured data the caller sends alongside it, and an unresolved number
fails the request rather than acting on the wrong report. And an ambiguous
request falls back to the read-only lookup, so a request the classifier is
unsure about can never register something.

Intent classification has a deterministic keyword-heuristic baseline that
always runs; when the three Azure OpenAI secrets below are provisioned, an
LLM call enhances it for phrasing the keyword list misses, overriding the
heuristic result only when it succeeds and returns a valid intent. Any LLM
failure (no secret, API error, malformed response) silently keeps the
heuristic classification - the step never hard-fails over an LLM outage, and
the template still runs and tests with no LLM configured at all. Field
extraction stays deterministic (regex/line structure). The shipped API
client uses a network-free stub transport by default; going live means
injecting real HTTP transports at construction, with no change to the
workflow itself.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |
| `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT` (optional secrets) | Enhances intent classification. Absent -> the deterministic keyword heuristic runs alone; the template still starts and works with none of these provisioned. |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. The agent imports its base classes from the framework package at start-up, so without that
package installed and configured, import and graph compile fail outright rather than leaving the
agent running in a partially working state. This is intentional — a half-running agent is worse
than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit and boundary tests
config/       agent manifest (agent.yaml) and runtime parameters (config.yaml)
docs/         design and test documentation
```

See `docs/02_design.md` for the architecture and `docs/03_test_spec.md` for the test
specification.

## Customising

1. Point `config/config.yaml` at your own Rakus tenant (`rakus.base_url`) and set the
   per-call budget (`timeout_s`).
2. Inject real HTTP transports into `RakusClient` (`src/services/rakus_client.py`) to
   replace the network-free stub, and provision the API token through the secrets
   provider.
3. Adjust the intent keywords and field extraction in `src/nodes/` for your own request
   wording.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

