# Template Design Specification — CMN-C2-280 Rakus Expense Agent

## Position in AgentCore Architecture

- **Agent Class**: `RakusExpenseAgent` (`src/graph/graph.py`)

| Field | Value |
|---|---|
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |

- **Category**: Cat 2 (multi-step domain workflow, ToolCallingAgent). Outer
  `AgentBaseGraph` 5-node backbone; the domain pipeline is encapsulated in a
  `GraphNode` (`main` slot) wrapping an inner `BaseGraph`
  (`src/graph/domain_workflow_graph.py`).
- **Agent type**: tool-calling — classify intent -> extract expense fields ->
  build a Rakus (RakuRaku Seisan) expense REST API request -> call the tool ->
  format the confirmation. No retrieval, no autonomous reasoning loop.
- **Three-Layer Separation**:
  - State: flat TypedDict `State(AgentState)` (no Pydantic — msgpack incompatible)
  - Node: framework inheritance (Template Method: `execute(self, state) -> dict` override only)
  - Graph: composition (`register_nodes()` + `super().register_nodes()`; `add_edges()`
    not overridden on the outer graph)

## Architecture Overview

### Outer graph — node configuration (`src/graph/graph.py`)

| Node | Responsibility | Input State | Output State | Trust | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------|-------------------|
| initialize | framework setup (schema, session, trust) | user_input | session/trust fields | framework default | InitializeNode (default) |
| pre_process | screen caller text + context; serialize request, report reference and caller fields into `validated_input` (JSON) | user_input, input_context | validated_input, report_hint | **VERIFIED_EXTERNAL** (the single external gate) | PreProcessNode (FunctionNode) |
| main | run the inner Rakus expense workflow subgraph | validated_input | result, intent, report_id, record_id, record_ref, expense_title, approval_status, confirmation, rakus_payload | GraphNode (caller ctx forwarded unchanged) | RakusWorkflowGraphNode (GraphNode) |
| post_process | shape caller-facing `formatted_output`; module-level `_security_gate_output()` scan; clear output on violation | inner-result fields | formatted_output | ANONYMOUS | PostProcessNode (FunctionNode) |
| finalize | framework finalize (metadata, timing) | — | response_metadata | framework default | FinalizeNode (default) |

### Inner workflow — node configuration (`src/graph/domain_workflow_graph.py`)

Inner graph inherits `BaseGraph` (fully custom linear topology). The 5 pipeline
steps map 1:1 to inner nodes. **Every inner domain node declares
`required_trust_level = TrustLevel.ANONYMOUS`** — the caller's
`InvocationContext` is forwarded into the subgraph unchanged, so the single
external trust gate stays on the backbone `pre_process`.

The topology is linear: `add_conditional_edges()` is not used anywhere. `route()`
exists because the base class declares it, and it is annotated with this graph's
own `State`. That annotation matters for any future conditional edge: the graph
runtime reads a path callable's annotation as its input schema and projects away
every field the annotation does not declare, so annotating with a broader base
type would make domain fields invisible to the router — and the branch would
silently never fire while unit tests on the function itself still passed.

| Inner node | Step | Responsibility | Output | Trust |
|------|------|---------------|--------|-------------|
| validate_input | 1 ValidateInput | empty/non-request guard; independent injection re-screen; deterministic (regex) flag-and-redact of email/token-like strings before logging | validated_input, report_hint, caller_fields, redaction_flags | ANONYMOUS |
| classify_intent | 2 ClassifyIntent | deterministic keyword classification -> lookup_expense / register_expense / check_approval, baseline always available; an optional Azure OpenAI call enhances it and overrides on success, silently falls back to the keyword result on any failure (no secret / API error / malformed response); low-confidence (from either path) -> lookup_expense (read-only default — never a write) | intent | ANONYMOUS |
| infer_rakus_fields | 3 InferRakusFields | merge caller-supplied fields with "Key: value" fields parsed from the text; resolve report number / expense title; assemble the Rakus expense API request body per intent; an unresolved report number is left empty (never invented) | expense_title, report_id, rakus_payload | ANONYMOUS |
| call_rakus_api | 4 CallRakusApi | GET /expenses (lookup) / POST /expenses (register) / GET /expenses/status (approval status) via `RakusClient`; token via ctx.secrets; 4xx/5xx -> status=error | record_id, record_ref, report_id, expense_title, approval_status | ANONYMOUS |
| confirm | 5 Confirm | format intent + record id + reference (+ approval status) into a human-readable confirmation | confirmation, result | ANONYMOUS |

### Data Flow

```
Outer:  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                              | (RETRY, max 3) ^
Inner (inside main / RakusWorkflowGraphNode):
        START -> validate_input -> classify_intent -> infer_rakus_fields
              -> call_rakus_api -> confirm -> END
```

Structured parameters travel as a JSON string: `pre_process` serializes
`{"text", "report_hint", "fields"}` into `validated_input`,
`RakusWorkflowGraphNode.extract_input()` hands that JSON to the subgraph, and
the first inner node (`validate_input`) parses it back. Inner nodes read
`state.get("validated_input") or state.get("user_input", "")`.

This serialization is also what carries the caller's structured data across the
outer/inner boundary. The subgraph node hands the subgraph a single input
string and does not forward the request context, so anything the caller sends
alongside the text has to be folded into that string by `pre_process` — which
is exactly what the `report_hint` and `fields` keys are for.

### State Definition (`src/schemas/state.py`)

All domain fields are declared `NotRequired[...]` (fields are absent until
their producer node writes them). Dict/list payloads
are stored as JSON strings (`Optional[str]`) via the module helpers
`to_json` / `from_json`, used by every producer and consumer.

| Field | Type | Purpose | Producer |
|-------|------|---------|----------|
| report_hint | NotRequired[str] | caller-supplied expense report reference, bound to the inert identifier alphabet; never inferred | pre_process / validate_input |
| caller_fields | NotRequired[Optional[str]] | JSON — bounded, inert (name, value) expense fields supplied by the caller | pre_process / validate_input |
| report_id | NotRequired[str] | resolved Rakus expense report number (v1: pass-through when the hint/text already carries a number) | infer_rakus_fields |
| redaction_flags | NotRequired[Optional[str]] | JSON list of pattern classes redacted before logging | validate_input |
| expense_title | NotRequired[str] | expense report title / record label | infer_rakus_fields / call_rakus_api |
| rakus_payload | NotRequired[Optional[str]] | JSON — assembled Rakus expense API request body (checkpoint-safe: stored as a JSON string via `to_json`/`from_json`) | infer_rakus_fields |
| rakus_config | NotRequired[Optional[str]] | JSON — runtime `rakus:` section forwarded by `_parent_config()` and injected via the inner graph's `_extra_initial_state()` | inner graph |
| rakus_timeout_s | NotRequired[float] | per-call Rakus budget from the same runtime config | inner graph |
| record_id | NotRequired[str] | expense report number / record id returned by Rakus | call_rakus_api |
| record_ref | NotRequired[str] | human-readable record reference (`rakus://expenses/<number>`) | call_rakus_api |
| approval_status | NotRequired[str] | approval workflow status returned by Rakus (check_approval intent) | call_rakus_api |
| confirmation | NotRequired[str] | human-readable confirmation | confirm |

`intent`, `result`, `validated_input`, `formatted_output` are inherited from
`AgentState` and are **not** re-declared.

**State Constraints (mandatory, satisfied):**
- Flat TypedDict only (primitives + JSON-serializable) — no Pydantic/dataclass.
- No JWT / API keys / credentials in State — the Rakus token is accessed via `ctx.secrets`.
- InvocationContext read via `InvocationContext.from_state(state)`, never stored in State.

## Configuration Contract

Configuration is split across two files, and the split is load-bearing:

| File | Contents | Read by |
|---|---|---|
| `config/agent.yaml` | the **flat** static manifest — `id`, `name`, `namespace`, `class`, `category`, `industry`, `generation_mode`, `required_trust_level`, `requires` | the agent registry, at discovery time |
| `config/config.yaml` | runtime parameters — `max_retry`, `timeout_s`, and the `rakus:` integration section | the graph, as `Graph(config=...)` |

The manifest is flat: every key sits at the root, with no `agent:` block. A
value nested under such a block would never be read, so `test_config.py`
asserts the absence of that block as well as the presence of the keys.

Nodes take **no constructor arguments** (nodes are no-arg; configuration never
rides on node instances). `RakusWorkflowGraphNode._parent_config()` reads
`config/config.yaml` and forwards the `rakus:` section and `timeout_s` to the
inner graph under `config["configurable"]`. The inner graph's
`_extra_initial_state()` then seeds State with `rakus_config` (JSON) and
`rakus_timeout_s`, where `CallRakusApiNode.execute(state, config=None)` reads
them and hands both to the client (an explicit
`config["configurable"]["rakus"]` override is also honoured for direct
invocation).

Every declared runtime value has a live consumer, and the tests prove it end to
end rather than at the node boundary: `max_retry` is consumed by the backbone's
own retry route, `rakus.base_url` and `timeout_s` are asserted to arrive inside
the client actually used for the call. A forwarding function that returns an
empty mapping is the failure mode this design guards against — it leaves every
declared setting silently dead while the suite stays green.

## Security Design

- **Trust gate** — the single external trust gate is on the outer backbone
  `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; every inner
  domain node — **including the write-capable `CallRakusApiNode`** — declares
  `TrustLevel.ANONYMOUS`. `GraphNode.execute()` forwards the caller's
  `InvocationContext` into the subgraph **unchanged** (no elevation), and
  `VERIFIED_EXTERNAL (1) < INTERNAL (2)`, so declaring an inner node `INTERNAL`
  would deny a legitimate external caller before the call runs — the boundary
  is therefore enforced exactly once, at `pre_process`. Agent-level default trust
  `VERIFIED_EXTERNAL` is declared in `config/agent.yaml`. `src/api/server.py`
  enforces the standalone entry-point Bearer-token auth boundary
  (`INVOKE_AUTH_TOKEN` -> VERIFIED_EXTERNAL elevation).
- **Caller-input contract** — `PreProcessNode` owns it, and the screens live
  there rather than relying on a framework gate in front of the node: a gate
  that is absent or configured off in some deployment would let the payload
  through to the answer path and return success, which is fail-open on exactly
  the decision the node exists to make. `ValidateInputNode` re-screens
  independently, so driving the inner graph directly cannot skip the check.

  - **Injection screen (fail closed).** Two classes are screened: chat-template
    control tokens (`<|…|>`, `[INST]`, `<<SYS>>`) and directive phrases. The
    token class is screened as a class rather than as a phrase list, because a
    payload that opens a system turn steers a model without containing any
    directive wording at all. Text is screened **both raw and after the markup
    strip**: the strip deletes control tokens (hiding a token attack from a
    post-only screen) and re-joins split directives such as `ig<b>nore` (hiding
    a spliced attack from a raw-only screen), so screening either form alone
    converts a detectable attack into an undetectable one. Input is normalised
    (compatibility folding, zero-width removal) before matching. The screen runs
    over the **parsed** request context depth-first, keys included — escape
    sequences are already decoded at that point, so a `\u`-escaped payload has
    nowhere to hide, and a hostile field name is caught rather than echoed.
    Directive patterns are anchored to verb + object pairs so ordinary expense
    prose ("please disregard the duplicate line item") is not refused; the
    suite probes both directions.
  - **Bounded, inert caller data.** The report reference is bound to
    `[A-Za-z0-9][A-Za-z0-9_-]{0,19}` — it reaches `record_ref`, the assembled
    request body and the confirmation line, so free text there would be
    caller-controlled output. A *present but malformed* reference is a
    rejection, never a silent fall-through to the next candidate key, which
    would act on a different report than the caller named. Field names are
    bound to `[a-z0-9_]{1,32}`; field count, value length and structure depth
    are capped; a rejected field is reported by POSITION, never by value.
  - **Numerics fail closed.** Any numeric a caller supplies passes
    `check_finite` before use. NaN and ±Infinity parse cleanly via `float()`
    and arrive through raw JSON, and every comparison against NaN is False — so
    an unchecked non-finite value silently suppresses the decision it feeds.
    The same rule covers the declared `timeout_s`. The suite carries a
    parametrized non-finite matrix per field plus rejections through `/invoke`.
  - **Flag-and-redact for logging.** `ValidateInputNode` additionally scans for
    email addresses and access-token-like strings (`eyJ...`, `secret_...`,
    `sk-...`) and redacts them before any logging. An expense request
    legitimately names amounts, vendors, and report numbers, so that scan is
    flag-and-redact rather than a reject; the framework's own PII mask
    additionally masks emails/phones/names in `user_input`/`validated_input`.
- **Secrets** — the Rakus integration token is read via
  `ctx.secrets.get("RAKUS_TOKEN")` (`InvocationContext.from_state(state)`),
  never `os.environ`, never stored in State. That lookup is deliberately the
  OPTIONAL form and is NOT declared in `requires.secrets`: the default
  transport is network-free and runs without a credential, and declaring a
  secret the deployment does not provision makes the agent fail to start. A
  missing token is tolerated only while that stub transport is active; with a
  live transport injected, a missing token is a hard `status=error` — a real
  API is never called unauthenticated. Separately, `ClassifyIntentNode`'s
  optional LLM enhancement resolves `AZURE_OPENAI_API_KEY` /
  `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT` via
  `ctx.secrets.require(...)` (the REQUIRED form) — all three ARE declared in
  `requires.secrets` alongside `requires.extras: [openai]`. Unlike the Rakus
  token, a missing one of these does not fail the request: it is caught inside
  `_classify_via_llm()`'s `except Exception` and the node keeps the keyword
  classification, so the compile-time gate enforces only that the secret NAME
  is legitimate, not that the LLM path is exercised.
- **Output gate** — the **module-level** `_security_gate_output()` in
  `src/nodes/post_process_node.py`, called from `PostProcessNode.execute()`.
  It blocks any SUCCESS response that lacks record evidence
  (record_id/record_ref) and any credential-shaped string anywhere in the
  caller-facing output. Two properties are worth stating explicitly, because
  each fails silently when it is missing:

  - It walks the output structure **recursively**, mapping KEYS included. A
    scan restricted to top-level string values reports zero findings on caller
    text nested inside the assembled request body, and a values-only walk
    reports zero findings on exactly the case where the credential IS the key —
    and zero findings is indistinguishable from clean, so the suite pairs the
    nested and key cases with a positive control that proves the scanner works
    at all and a negative control that proves it is not simply flagging
    everything. A violation names the offending PATH only; a credential-shaped
    key is withheld from that path (`<withheld>`) rather than quoted by the
    label reporting it, because the label travels in `error_log`, where the
    framework's own credential scan would raise on it and replace this node's
    cleared result with a bare error — re-opening the fallback the clearing
    just closed.
  - On violation it **clears every output-bearing state field**. Returning an
    error status (or raising) while leaving the inner answer in state is not
    containment: the framework builds its response from `formatted_output` or,
    failing that, `result`, regardless of status — so the un-gated answer would
    ship inside the error envelope.

  **The error envelope is a closed set.** EVERY non-success return goes through
  the one module-level `_contain()` helper, and `formatted_output` is
  `{"reason": <code>}` and nothing else, with the code drawn from the module's
  own `ERROR_REASONS` — `rakus_workflow_failed` (the inner workflow reported an
  error) or `output_withheld_by_gate` (the output gate refused the response).
  Nothing is read from `error_log` or from the gate's violation entries: those
  lines can embed an upstream Rakus error body, a report number or a
  caller-derived fragment, and truncating, path-stripping or credential-only
  redaction is not a closed set. The reason key keeps the envelope TRUTHY, so
  the `formatted_output or result` projection stops there. `error_log` stays
  the internal channel (state reducer + audit trail) and is never projected to
  the caller; gate violations are written there, and the inner entries are not
  re-emitted (the reducer appends). `CallRakusApiNode` keeps to closed-set
  labels for the same reason — what was not found, `Rakus API error <status>`,
  `Rakus call failed (<ExceptionClass>)` — because the audit trail reads
  `error_log`, a checkpoint keeps it, and the framework's egress scan raises on
  credential-shaped text in any node result.

  **Rejections are a closed set by construction.** `InputRejected` carries the
  offending class as a typed `code` constrained to `REJECTION_CODES`, plus an
  optional module-authored location, and `PreProcessNode` /
  `ValidateInputNode` rebuild the refusal notice from those two rather than
  interpolating the caught exception. The notices are byte-identical to before;
  what changes is that a future raise site cannot smuggle a caller value into
  one.

  No node defines `_extra_security_gate_input/_output` instance methods
  (framework hooks are @final / auto-wrapped — domain checks live inline or in
  module-level helpers).

  **Numeric output invariant — not applicable.** This template renders no
  monetary aggregates: its output is record identifiers, an intent label, an
  approval status, a confirmation line and the echo of the request body it
  submitted. There is therefore no rounding grid to enforce, and deliberately
  no numeric-rewriting pass over the output — such a pass would corrupt the
  report identifiers this agent exists to name. The invariant this boundary
  DOES enforce is the one stated above: no success without record evidence, and
  no credential-shaped value at any depth.
- **Audit** — every node's `execute()` emits exactly one positional
  `emit_trace_event("<node>_complete", {small non-PII payload}, state)` on its
  SUCCESS path (intent / presence signals only — never request text, expense
  content, or credentials). `__call__()` is never overridden; `_invoke_impl`
  is never defined on any node. Event names (documented for operations):

  | Node | Audit event |
  |------|-----------|
  | pre_process | `pre_process_complete` |
  | validate_input | `validate_input_complete` |
  | classify_intent | `classify_intent_complete` |
  | infer_rakus_fields | `infer_rakus_fields_complete` |
  | call_rakus_api | `call_rakus_api_complete` |
  | confirm | `confirm_complete` |
  | post_process | `post_process_complete` |

  Rejection and containment paths emit their own events —
  `pre_process_rejected`, `validate_input_rejected`,
  `post_process_gate_blocked`, `post_process_error_contained` — each carrying
  the offending CLASS or a count, never the offending value.

## Implementation Note — optional model call (intent classification only)

Field extraction (`InferRakusFieldsNode`) stays fully deterministic (regex and
line structure) — no model involvement, no change from the original design.

Intent classification (`ClassifyIntentNode`) has a deterministic keyword
heuristic as its BASELINE, which always runs and is what the template uses
and tests with no LLM configured at all. When the three Azure OpenAI secrets
(`AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT`)
are provisioned, the node also calls Azure OpenAI (via `AzureOpenAIClient`,
built fresh per invocation from `ctx.secrets`, never cached) to classify the
same request, and OVERRIDES the keyword result when the call succeeds and
returns a valid intent. Any failure — secret not provisioned, API error,
malformed/wrong-shape response — is caught and silently discarded; the
heuristic classification already computed is kept, and the node never raises
or sets `status=error` over an LLM outage. The manifest declares
`generation_mode: llm` and `requires.secrets`/`requires.extras` for the three
secrets + the `openai` extra (`AzureOpenAIClient` wraps
`langchain_openai.ChatOpenAI`). Natural-language expense summaries would be a
further, separate enhancement and are out of scope here.

## Limitation — Rakus client (documented)

`src/services/rakus_client.py` is a plain service-layer client (injectable
transport, `RakusApiError`, per-call token and budget, no framework imports)
that ships a **deterministic, network-free stub** as its default transport: it
returns the documented Rakus (RakuRaku Seisan) expense API response shapes (an
`expense_data` list for lookups; an `approval_status` record for status
checks; a task/receipt shape with a synthetic report-number echo for
registration, derived from the request) so the pipeline is runnable and
testable without a live Rakus tenant or an HTTP library. It does **not**
perform a live Rakus call — the limitation is documented rather than papered
over with a faked one.

To go live, inject real `post`/`get` transports at construction. Every
transport is called as `(url, headers, json_body, timeout_s)`, so a live
adapter receives the configured per-call budget and can hand it straight to its
HTTP library; the method contracts and payload shapes are documented per
endpoint, so no business-logic change is required. (The stub also runs without
a live credential — see Secrets above; a live transport requires
`RAKUS_TOKEN`.)

## Framework Utilization

### Shared Components Used
- [x] InvocationContext — read in `CallRakusApiNode` via `InvocationContext.from_state(state)` (secrets + trust)
- [x] Trust gate — single external gate `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; inner domain nodes (incl. `CallRakusApiNode`) declare `TrustLevel.ANONYMOUS` (caller `InvocationContext` forwarded unchanged into the subgraph)
- [x] Secrets — `ctx.secrets.get("RAKUS_TOKEN")` (optional lookup, not declared); `ctx.secrets.require(...)` for the three Azure OpenAI secrets in `ClassifyIntentNode` (declared in `requires.secrets`, caught by the node's own `except Exception` on a miss); entry-point `bound_secrets` / `secrets_factory` / `provision_secrets` in `src/api/server.py`
- [x] `emit_trace_event()` — one positional call per node on the SUCCESS path, plus rejection/containment events; framework lifecycle events are NOT re-emitted

### Composition Pattern

- **Pattern**: GraphNode (subgraph) — Cat 2 outer/inner split.
- **Composition target**: inner `RakusWorkflowGraph` (`BaseGraph`) via `RakusWorkflowGraphNode.get_subgraph()`.
- **Config forwarding**: `RakusWorkflowGraphNode._parent_config()` reads
  `config/config.yaml` and forwards `{rakus, timeout_s}` under
  `config["configurable"]` to the subgraph.
- **Error propagation strategy**: `propagate` (default) — inner errors re-raised as
  `SubgraphError`; per-step `status=error` + `error_log` for API/validation failures
  (no silent pass).

## Import Isolation Confirmation
- [x] Template imports `framework/` and `shared/` only; no platform-internal import anywhere
- [x] `src/services/rakus_client.py` and `src/services/security.py` have no
      framework imports (pure service layer, stdlib only)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Framework base class | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed multi-step pipeline, not an autonomous loop |
| Composition pattern | flat single main node | GraphNode + inner subgraph | GraphNode + inner subgraph | 5 domain steps live in the inner graph; a flat shape would put them all in one node |
| Model dependency | model client in the pipeline | deterministic classification + extraction | deterministic baseline + optional LLM override on `classify_intent` | keyword heuristic always available and is what runs/tests with no LLM configured; Azure OpenAI enhances classification and overrides on success, never fails the request on any LLM failure; field extraction stays deterministic (no model involvement) |
| Rakus client | live HTTP call | injectable transport + documented stub default | injectable + stub default | no live network from the shipped default; the limitation is documented rather than faked; go-live is a transport injection, no logic change |
| Node configuration | ctor-arg dependency injection | no-arg nodes + runtime-config forwarding via `_parent_config()` -> `configurable` -> state | no-arg nodes | nodes are no-arg (ctor args raise at graph build); the config files stay the single source |
| Where the input screens live | rely on the framework input gate | enforce in the node that owns the caller contract | in the node | a framework gate that is absent or configured off would fail OPEN; the node's own screen is provable by calling `execute()` directly |
| Gate violation handling | raise | return error and clear output-bearing fields | clear the fields | the framework falls back to `result` even on an error status, so raising alone still ships the un-gated answer |
| Write target | infer report number from NL freely | caller-supplied/explicit number only; unresolved left empty | explicit only | never touch the wrong expense report; unresolved number -> status=error, not invented |
| Default intent | register_expense | lookup_expense | lookup_expense | low-confidence classification must never default to a write |
| Keyword priority | writes first (register > status > lookup) | check_approval > register_expense > lookup_expense | check_approval first | approval-status wording ("承認", "status") often co-occurs with registration verbs ("申請"); a status question must never trigger a write, and a pure registration request carries no approval keyword |
