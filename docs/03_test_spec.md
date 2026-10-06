# Test Specification - CMN-C2-280 Rakus Expense Agent

## Test Strategy
- Test types: Unit (per node + service + config + caller contract + output gate +
  inner graph) / Proof-of-Boundary (full outer-graph invoke, the real ASGI entry
  point, import isolation, state safety, server boot, HITL stub).
- Location: `tests/unit/`, `tests/proof_of_boundary/` (`tests/integration/` is an
  empty package; end-to-end coverage lives in the boundary suite, which drives the
  real compiled outer graph and the real HTTP app).
- The Rakus call is exercised through the deterministic, network-free stub
  transport (default) and through monkeypatched fake clients; no live Rakus call.
- `ClassifyIntentNode`'s optional Azure OpenAI enhancement is exercised only via
  constructor-injected test-double `llm=` objects (`complete(messages) -> {"content": ...}`);
  no real Azure OpenAI call anywhere in the suite.
- **Trust-gate routing canon**: every per-node unit test invokes the node as
  `node(state)` - `BaseNode.__call__` routes the full security pipeline (trust
  gate -> PII mask -> `execute()` -> credential scan) - never bare
  `node.execute(state)`. State builders set `caller_trust_level =
  TrustLevel.VERIFIED_EXTERNAL.value` for PreProcessNode (the single external gate)
  and `TrustLevel.ANONYMOUS.value` for every other node. The trust-rejection test
  asserts on the RETURNED error dict (`status == AgentStatus.ERROR.value`, "trust
  gate denied" in `error_log`, execute-only keys absent) - `__call__` never raises
  for a trust denial.
- **Documented exceptions to that canon**, both deliberate:
  `CallRakusApiNode.execute(state, config=...)` (a 2nd argument `__call__` cannot
  forward), and the whole of `test_caller_contract.py`, which calls `execute()`
  DIRECTLY. The second one is the point of that file: a screen asserted through
  the framework wrapper proves only that *something* refused, and would still
  pass in a deployment where the wrapper's own gate is absent or configured off.
  Calling `execute()` directly proves the template owns the refusal.
- **Assertions are behavioural, never gate wording**: a refusal is asserted as
  error status plus nothing carried forward, so the suite keeps its meaning
  across framework versions rather than pinning a particular message.
- Assertion contract: the invoke surface is
  `result["output"]` / `status` / `trace_id` / `correlation_id` / `node_history`
  (never `formatted_output` at the invoke surface); status is compared to
  `AgentStatus.SUCCESS`/`.value` (lowercase `success`/`error`); the outer graph is
  called as `invoke(user_input=..., ctx=...)`; identifiers may be masked
  (`[MASKED]`) so record evidence is asserted by presence, not raw repr; audit
  spies assert on `call.args[1]` (the event payload), never the whole-call repr.
- Framework pipeline behaviours encoded by the suite: `__call__` short-circuits on an
  incoming errored state (execute() is skipped; error status/error_log pass through);
  the framework input mask rewrites Title-Case bigrams (across newlines), emails, and
  digit groups in `user_input`/`validated_input` to `[MASKED]` before `execute()`
  sees the text - positive payloads are PII-free, intentional-PII tests assert the
  `[MASKED]` path.
- Domain audit events are muted per module via an autouse fixture patching
  `src.nodes.<mod>.emit_trace_event` (never a `sys.modules` stub of `shared.*`).

## Unit Tests (`tests/unit/`)

| TC-ID | Test file | Focus | Expected |
|-------|-----------|-------|----------|
| U-01 | test_trust_gate.py | Trust boundary: ANONYMOUS caller on the VERIFIED_EXTERNAL pre_process gate; inner nodes ANONYMOUS; trust-posture declarations; manifest `required_trust_level` matches the PreProcessNode gate (no code/config drift) | denial RETURNS error dict (`status == AgentStatus.ERROR.value`, "trust gate denied" in error_log, execute-only keys absent); VERIFIED_EXTERNAL passes; every inner node declares ANONYMOUS; manifest == `VERIFIED_EXTERNAL` |
| U-02 | test_pre_process_node.py | serialize request text + report reference + caller fields into `validated_input` (JSON); markup strip; reference priority report_number > report_id > report_hint | reference resolved by priority; `<script>` stripped; empty/missing -> `status=error` |
| U-03 | test_validate_input_node.py | empty/short guard; JSON-shaped input; framework `[MASKED]` path for emails; node-level token flag-and-redact (`secret_*`) | email -> `[MASKED]` before execute; token -> `[REDACTED]` + `redaction_flags=["token"]` (JSON string); empty/short -> error; audit payload carries flags only |
| U-04 | test_classify_intent_node.py | intent = lookup_expense / register_expense / check_approval (keyword, check_approval-first priority - a status question never triggers a write; read-only default); optional Azure OpenAI enhancement (test-double `llm=` injection, no real network call) overrides the keyword result on a valid response and silently falls back to it on any failure | correct intent per keyword; approval wins over register; no-signal defaults to lookup_expense with non-fatal note; empty -> error (LLM never called); audit emits intent label + `llm_inferred` flag only; LLM valid JSON overrides keyword result; prose/markdown-fenced JSON still parses; malformed/wrong-shape JSON -> keyword result kept; LLM raising -> keyword result kept; no `llm=` injected and no secret bound -> keyword result kept (real production shape with no Azure key configured) |
| U-05 | test_infer_rakus_fields_node.py | report-number resolution (text > number-shaped reference; never invented); quoted title; caller-supplied fields merged ahead of `Key: value` fields parsed from text; Rakus `expense_data` payload for register vs read-only `{report_number}` for lookup/check_approval (JSON string) | lookup/status `{report_number}`; register `expense_data[0]` with number/title/items; unresolved number left `""`; empty input -> error |
| U-06 | test_call_rakus_api_node.py | lookup/register/check_approval via the network-free stub; `rakus_config` / `rakus_timeout_s` state fields + `execute(state, config=...)` override (documented direct-execute exception); API error / empty lookup result / unresolved number / unknown intent / missing payload; secret posture (live transport refuses to run unauthenticated; token read via `ctx.secrets`, never env/state) | record_id/record_ref on success; approval_status `pending` on the stub status path; 403 surfaces in error_log; empty `expense_data` -> "no expense report found"; live+no-secret -> error "unauthenticated"; live+bound secret -> token passed to client; audit emits presence signals with `stub_transport=True`; transport failures report the exception CLASS, never its text |
| U-07 | test_confirm_node.py | human-readable confirmation per intent verb; ref/id/approval formatting; title fallback | "Retrieved/Registered/Checked approval status of expense report ... ref=... id=... [approval=...]"; missing evidence -> error |
| U-08 | test_post_process_node.py | `formatted_output` shaping (payload round-trip); errored state passes through `__call__` un-masked (short-circuit); record-evidence gate (module-level `_security_gate_output()` helper, full-node path + direct function tests incl. credential-shaped value) | success shape with parsed `rakus_payload`; error status/error_log preserved, no success shape fabricated; SUCCESS without record evidence blocked |
| U-09 | test_rakus_client.py | Rakus (RakuRaku Seisan) expense client: find/create expense + approval status; `Authorization: Bearer` header; `RakusApiError` on non-2xx; stub shapes (expense_data echo / approval `pending` / task receipt + expense_number echo, `_stub` marker); `uses_stub_transport`; the per-call budget reaches the transport, and a non-finite/unusable/oversized budget falls back or clamps | correct URLs/headers/bodies/timeout; 400 raises with joined `errors`; stub deterministic shapes; NaN/Inf/0/negative/bool/str budget -> default; oversized -> clamped |
| U-10 | test_config.py | manifest + runtime-config sanity, kept as SEPARATE contracts | manifest is FLAT (no `agent:` block) with id CMN-C2-280, Cat 2, CMN, ToolCallingAgent, dotted `class` entry point, VERIFIED_EXTERNAL, `requires.secrets == []` and `requires.extras == []`; `config/config.yaml` carries max_retry, timeout_s and rakus.base_url |
| U-11 | test_domain_workflow_graph.py | inner `RakusWorkflowGraph`: identity, `_extra_initial_state()` rakus_config/timeout injection, `route()` error short-circuit and its State annotation, `get_output` contract, compile, direct inner invoke on the stub | name/state_schema correct; config forwarded as JSON string; error -> END; inner invoke runs validate -> classify -> infer -> call -> confirm to SUCCESS with record evidence |
| U-12 | test_caller_contract.py | the caller-data contract, asserted by calling `execute()` DIRECTLY: chat-template control tokens (`<\|…\|>`, `[INST]`, `<<SYS>>`) and directive phrases; the raw-vs-sanitized pair (a control token that the markup strip DELETES, and a spliced directive it RE-JOINS); hostile field name; `\u`-escaped payload; nested payload; inert report-reference and field-name alphabets; count/length/depth caps; per-field non-finite matrix; the inner node's independent re-screen | every carrier refused with error status and nothing carried forward; rejections name the CLASS or the POSITION, never the value; six real expense sentences (EN + JA) are NOT refused |
| U-13 | test_output_gate_containment.py | the output boundary: nested-structure walk with positive AND negative controls; violation clears every output-bearing field; the error envelope carries no released text, no source paths, no credential-shaped value; a clean success is still returned | nested credential found (top-level control proves the scanner works, clean control proves it is not flagging everything); `result` cleared to None; envelope free of the inner answer; a source path and a credential-shaped `error_log` entry produce the same constant envelope, and the inner entries are not re-emitted |

## Proof-of-Boundary Tests (`tests/proof_of_boundary/`)

| PB-ID | Boundary | Test | Expected |
|-------|----------|------|----------|
| PB-4 | Import isolation | test_import_isolation.py | AST scan of `src/`: 0 platform-internal imports |
| PB-2/PB-5 | State serialization | test_state_safety.py | `state.py`: no Pydantic/credential fields |
| PB-6 | Backbone invoke-order + external-trust | test_pb_invoke_order.py | `_VALID_PAYLOAD` byte-equal to `deploy/invoke_payload.json` "input" (asserted); VERIFIED_EXTERNAL caller yields `status=success` with `node_history == [InitializeNode, PreProcessNode, RakusWorkflowGraphNode, PostProcessNode, FinalizeNode]` and record evidence + confirmation in `result["output"]`; ANONYMOUS caller denied at pre_process (error, no post_process, no output); blank input -> error, not crash |
| PB (invoke) | Caller-data contract through the REAL ASGI app | test_invoke_caller_data.py | Bearer auth (missing/wrong token -> 401 with a body that does not say which); a report reference supplied ONLY in `input_context` produces a real answer about that report; caller fields reach the assembled request body; all three intent paths reachable; absent context degrades to the text-only path; malformed context and injection payloads refused with nothing published; a non-finite JSON literal refused; oversized input -> 422; too many context keys -> 400; response scanned for credential shapes and the error response for source paths |
| PB-7 | HITL interrupt propagation *(conditional)* | test_pb7_hitl_interrupt_propagation.py | **Auto-waived - non-HITL** (config/agent.yaml has no `hitl.enabled: true`): module-level skipif; stub bodies are real AssertionErrors so enabling HITL without implementing PB-7 fails loudly |
| PB (boot) | Server entry point | test_server_boot.py | importing `src.api.server` does not raise (construct + compile + provision_secrets at import); agent constructs + compiles via the supported path; `/invoke` + `/health` routes exposed |
| PB (closed-set envelope) | What the caller-visible ERROR envelope is made of | test_error_envelope_closed_set.py | Parameterised over EVERY non-success path of `PostProcessNode.execute()` (inner-workflow error; gate refusal for missing record evidence, a credential in a narrative field, a credential nested in the payload, a credential carried as a payload KEY), each with a sentinel upstream line already seeded in `error_log`: `formatted_output == {"reason": <one of ERROR_REASONS>}`, truthy, every value drawn from the declared constants; the sentinel appears nowhere in the returned mapping (keys and values walked at every depth); `result` cleared; the inner entries not re-emitted. `ERROR_REASONS` holds exactly the two codes; a credential-shaped mapping key — local pattern AND framework-only shapes such as `AKIA…` — is withheld from the violation label, which rides `error_log` only. Through the real ASGI `/invoke` with Bearer auth: a clean request still succeeds (control); a gate refusal yields `output == {"reason": "output_withheld_by_gate"}`, no `error_log` key, no gate-violation wording and no sentinel anywhere in the body; an inner-workflow error yields `status=error` with `PostProcessNode` absent from `node_history` (the backbone routes to finalize) and the sentinel nowhere in the body. The walker used by the absence assertions is itself falsified first (nested value, nested key, non-string leaf) |

> PB-1 (audit emission) is covered inside the unit suite via the emit-spy tests
> (validate / classify / call nodes assert on the event payload,
> `call.args[1]`). PB-3 (live external service) is exercised only against a real
> tenant, not in this suite - the shipped transport is the documented
> network-free stub.

## Test Execution Summary
- Total tests: 254
- Pass: 252 / Fail: 0 / Skip: 2 (PB-7 A/B - auto-waived, non-HITL)
- Run against the real framework wheel, not a stub package.
