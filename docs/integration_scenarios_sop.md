# Continuous Integration Scenarios SOP — mcp_a2a_proxy

## 1. Document Control

| Field | Value |
|---|---|
| SOP title | MCP A2A Proxy — Live Integration Scenarios SOP (P4) |
| Version | 0.4.0 |
| Owner / contact | SilvaEngine module owner (name on sign-off) |
| Last updated | 2026-09-02 |
| Business domain | generic (AI-agent infrastructure / MCP-A2A adapter) |
| Target environment | dev (local gateway on `localhost:8765`) — never `production` without explicit approval |
| Approval status | approved (owner confirmed 2026-08-30 for the end-to-end run; v0.4.0 revises scenarios for daemon baseline `d22a9f7` — re-confirm before the next live run) |

> Pre-filled from project analysis and owner decisions (latest 2026-09-02): `docs/DEVELOPMENT_PLAN.md` §§1–9, README §"Tool Surface", `pyproject.toml`, unit suite (`test_reconciliation.py`, `test_tools.py`, `test_a2a_client.py`), `a2a_daemon_engine/models/postgresql/a2a_agent.py`. Owner-confirmed: **Hermes registered directly in `a2a_daemon_engine` and routed via its handler `agent_type` shorthand — NOT through `docker-a2a-hermes-agent-gateway`**; core-engine bridge as second peer; **OpenClaw removed from scope**; **PostgreSQL is the sole persistence backend under test** (registry reads verified from PostgreSQL `a2a_agents`); credentials from MCP registry settings; manual CI runs for now. **v0.4.0 change:** aligned with `a2a_daemon_engine` Phase 12–14 (baseline `d22a9f7`, 2026-09-02) — handler module renames, authenticated extended card, multimodal Parts capture, `AUTH_REQUIRED` mapping, and the new `a2a_proxy` peer type; known daemon defects from the 2026-08-30 certification runs are annotated on the affected scenarios (DEF-001…008 in `docs/test_results/integration_certification_report.md`).

## 2. Purpose and Scope

This SOP governs P4 (Live Integration) of `mcp_a2a_proxy` 0.1.0: certify that the eight-tool MCP→A2A adapter works end to end against the live `silvaengine_gateway` + `a2a_daemon_engine` stack and its deployed peers, closing the P4 gate defined in `docs/DEVELOPMENT_PLAN.md` §8 and feeding the P5 release certification report under `docs/test_results/`.

- **In scope:** the 8 MCP tools of `MCPA2AProxy` (discovery: 3, delegation: 2, task tracking: 3), the JSON-RPC/GraphQL/REST call paths in `a2a_client.py`, auth lifecycle, error mapping, and the P4 live scenarios 1–9 in `DEVELOPMENT_PLAN.md` §9.
- **Out of scope:** daemon-side admin CRUD, operator surfaces, MCP transport/dispatch (`mcp_daemon_engine`), SSE client lifecycle, A2A protocol semantics beyond what the tool contract surfaces, task persistence internals, push-notification webhooks (daemon Phase 13 made them durable + sent, but the calling agent has no reachable webhook endpoint — still out of scope), `resubscribe_a2a_task` (deferred to 0.2.0), remote peer card fetch (deferred, §5.1a). **This SOP does not certify `silvaengine_gateway` or `a2a_daemon_engine` as components** — they are validated only insofar as they serve this module's client paths.
- **System(s) under test:** `mcp_a2a_proxy` (target module) and its dependency platform: `silvaengine_gateway` → `a2a_daemon_engine` (baseline `d22a9f7`, Phase 14 complete) → peer handlers (Hermes registered in the daemon; core-engine bridge; `a2a_proxy` peers eligible).

## 3. Environment and Access

| Item | Value / source |
|---|---|
| Environment target | local dev (`localhost:8765`) — gateway **currently stopped** as of 2026-09-02 review; must be restarted on the updated daemon baseline before the next run |
| Daemon baseline | `a2a_daemon_engine` @ `d22a9f7` (Phase 14 complete, 2026-09-02); the 2026-08-30 certification runs executed against the pre-Phase-13 checkout — P4 scenarios must re-execute against the new baseline before release |
| Base URLs / endpoints | `http://localhost:8765/{endpoint_id}/a2a` (JSON-RPC); `http://localhost:8765/{endpoint_id}/a2a_core_graphql` (GraphQL); `http://localhost:8765/{endpoint_id}/.well-known/agent-card.json` (REST); `http://localhost:8765/auth/token` (password grant) |
| Credential source | gateway `.env` (`ADMIN_USERNAME`/`ADMIN_PASSWORD`) or per-partition MCP registry settings, read at runtime; never inlined in scripts or reports (as executed 2026-08-30) |
| Required env vars | names only: `A2A_STREAM_TIMEOUT`, `HERMES_STREAM_TIMEOUT`, `OPENCLAW_STREAM_TIMEOUT`, `CORE_ENGINE_STREAM_TIMEOUT` (gateway-side bounding; the proxy reads none) |
| Data stores | **PostgreSQL** — daemon registry (`a2a_agents`) and task store; registry reads are verified against the PostgreSQL tables (SQLAlchemy models in `a2a_daemon_engine/models/postgresql/`), per owner decision 2026-08-29 |
| Messaging / events | none consumed by this module (daemon-internal event streaming; SSE is gateway-owned) |
| Access constraints | local dev — open access assumed (verified: gateway probe 401 = healthy + auth-gated) |
| Provisioning policy | auto-provision when safe (test agents/tasks only; no schema changes; peer registration is an **ops prerequisite**, see §4) |

## 4. Dependency Readiness Requirements

> Each dependency must reach all four readiness states before testing begins:
> `available -> configured -> initialized -> operational`.

| Dependency | Type (internal / infra / external) | Health check | Required readiness | Owner |
|---|---|---|---|---|
| `silvaengine_gateway` (localhost:8765) | internal | HTTP reachable; auth endpoint responds | operational | platform owner |
| `a2a_daemon_engine` (registered gateway module) | internal | JSON-RPC `agent/getAuthenticatedExtendedCard` or agent-card REST returns 200 | initialized | platform owner |
| PostgreSQL (SilvaEngine datastore) | infrastructure | SQLAlchemy connection succeeds; `a2a_agents` table present; discovery query returns registered peers | initialized | platform owner (registry reads verified from PostgreSQL per owner decision) |
| Peer: Hermes Agent (registered in `a2a_daemon_engine`, routed via `agent_type: "hermes"` shorthand — handler module renamed to `hermes_handler.HermesAgentHandler` on 2026-09-02, commit `6bdad9a`) | external | appears in `discover_a2a_agents` with status active | operational | owner (confirmed 2026-08-29) |
| Peer: `ai_agent_core_engine` bridge (`agent_type: "core_engine"` → `core_engine_handler.CoreEngineAgentHandler`) | internal | reachable via daemon default routing or discovery | operational (delegation P1) | owner (confirmed 2026-08-29) |
| Peer: `a2a_proxy` type (Phase 14) — optional third peer if an A2A-compliant backend is registered | internal/external | appears in discovery; delegates through `A2AProxyHandler` with per-agent metadata (`a2a_proxy_url`/`token`/`timeout`) | optional (P2 if exercised) | platform owner |

> **Registry hygiene (v0.4.0):** handler resolution prefers explicit `metadata.module_name`/`class_name` over the `agent_type` shorthand (`a2a_ai_agent_utility.py:289-313`). Rows still carrying the **pre-rename** `module_name` (e.g. the test partition's `openclaw-agent` holds `...handlers.a2a_openclaw_handler`) bypass the renamed map and will fail handler import — re-register or clear `module_name`/`class_name` and set `agent_type` before the run. Verify via the PostgreSQL registry read.
| Auth provider (gateway `/auth/token`) | internal | password grant issues JWT; `x_api_key` fallback works | configured | platform owner (credentials via MCP registry settings) |

**Blockers reported as gaps, not failures:** unregistered/unreachable peers do not fake-pass — the corresponding scenarios are reported `blocked` with root cause, and certification is capped accordingly (§10, §12). **OpenClaw is excluded from scope** (owner decision): any OpenClaw registry entry encountered is out of scope and ignored, not a failure.

## 5. Test Data Requirements

| Asset type | Count | Notes / constraints |
|---|---|---|
| Registered A2A peer agents | 2 (+1 optional) | Hermes (`agent_type: "hermes"`) + core-engine bridge (`agent_type: "core_engine"`); optional `a2a_proxy` peer if a backend is registered. `status=active`, in test partition; `capabilities` may be NULL — that itself is a scenario-1 assertion. Registry rows verified against PostgreSQL `a2a_agents` **including handler-resolution metadata hygiene (post-rename `module_name` or `agent_type`)** |
| Delegatable prompts | ≥ 6 | One per delegation scenario: trivial complete, stream-complete, multi-turn approval, long-running (>~60s, for cancel/stream-timeout), plus negative-payload prompts |
| Bogus identifiers | 2 each | unknown `agent_id`, unknown `task_id` — for `AGENT_NOT_FOUND` / `TASK_NOT_FOUND` mapping |
| Task partition | 1 dedicated `part_id` | all test delegations run inside it; wrong-part ID isolation is a scenario-1 assertion |
| Users / roles | 1 test credential | service account (`token_username`/`token_password` or `x_api_key`) from the approved secret source |

- **Load order:** gateway up → daemon module initialized → peers registered (ops prerequisite; per dev plan §5.1 the registry list query is **cached**, so registration must precede the run with cache TTL margin) → test prompts created at runtime (no fixture store needed; delegation payloads are generated).
- **Data source:** generate realistic at runtime; peers and partition are **pre-existing deployment artifacts** — no live third-party side effects beyond task rows created inside the test partition (cancelled test tasks deliberately leave cancelled rows; idempotent per §9 scenario 7).

## 6. Execution Order

```text
Foundation (unit reconciliation) -> Discovery -> Capability read -> Delegation -> Streaming -> Multi-turn -> Recovery -> Cancellation -> Failure modes -> Auth lifecycle
```

**Reason for deviation from the skill default sequence (Foundation → Master Data → Customer → … → Billing):** the default is ecommerce-order-domain; this module's dependency graph is an MCP→A2A adapter whose surfaces are discovery/delegation/task-tracking, so the order derives from the client loop (discover → delegate → track → abandon) and DEVELOPMENT_PLAN §9 scenario numbering 1–9. Delegation scenarios (3–5) run on the **PostgreSQL** task store (owner decision 2026-08-29, superseding the dev plan's dual-backend requirement). **Scope note:** Hermes is exercised via its daemon-registered handler (`agent_type: hermes`); OpenClaw scenarios are deferred; an optional `a2a_proxy` peer (daemon Phase 14) may be added when a backend is registered.

## 7. Integration Scenarios

> Mapped 1:1 from DEVELOPMENT_PLAN.md §9 (P4 live scenarios). Priority drives execution when time is limited (P1 = must pass to certify).

### Scenario template

| Field | Value |
|---|---|
| **ID** | P4-n («same numbering as DEVELOPMENT_PLAN §9») |
| **Name** | … |
| **Priority** | P1 / P2 |
| **Type** | end-to-end (API tool-call paths over live HTTP) |
| **CI trigger** | manual (nightly/pre-release, per §11) |
| **Preconditions** | §4 dependencies at required readiness; §5 test data present |
| **Dependencies** | per-scenario, listed |
| **Test data** | per-scenario, from §5 |
| **Steps / Expected / Validation / Cross-checks** | as tabled per scenario below |

### P4-1 — Discovery

| Field | Value |
|---|---|
| **ID** | P4-1 |
| **Name** | Peer discovery, filters, capabilities parsing, tenant isolation |
| **Priority** | P1 |
| **Type** | end-to-end (GraphQL path) |
| **CI trigger** | manual (nightly/pre-release, per §11) |
| **Preconditions** | Hermes (daemon-registered) + core-engine bridge active; cache TTL elapsed since registration; known `part_id` |
| **Dependencies** | a2a_daemon_engine (GraphQL `a2aAgentList`), persistence backend |
| **Test data** | registered peers; one wrong `part_id` value |
| **Steps** | 1. `discover_a2a_agents()` → assert Hermes + core-engine bridge present. 2. `discover_a2a_agents(status="active")` → same. 3. `discover_a2a_agents(agent_name=<substring>)` → filtered result. 4. Re-call with wrong `part_id`. 5. If any peer has populated `capabilities`, assert it arrives as parsed list; assert NULL capabilities peers surface no `capabilities` key (not `[]`). |
| **Expected behavior** | Peer list returned decamelized; filters apply; wrong partition returns no peers (tenant isolation); NULL capabilities stays absent without erroring. |
| **Validation points** | `a2a_agent_list`, status GSI filter used, capabilities parse/no-op, part-Id isolation |
| **Cross-system checks** | Registry row (PostgreSQL `a2a_agents`) matches GraphQL surface for name/status/metadata. |

### P4-2 — Capability read

| Field | Value |
|---|---|
| **ID** | P4-2 |
| **Name** | Daemon agent card (REST + extended JSON-RPC), boundary assertion |
| **Priority** | P1 |
| **Type** | end-to-end (REST + JSON-RPC paths) |
| **CI trigger** | manual (nightly/pre-release, per §11) |
| **Preconditions** | daemon initialized; configured auth |
| **Dependencies** | a2a_daemon_engine agent-card route; auth provider |
| **Test data** | none beyond credentials |
| **Steps** | 1. `get_a2a_agent_card()` → assert protocol version `1.0.0` and name (endpoint URL may remain daemon-config-level — substitution deviation DEF-008 recorded, not a proxy defect). 2. `get_a2a_agent_card(extended=true)` → **Phase 13 (C5): the extended card is now a real authenticated document** (traceability extension + docs URL), auth-gated — assert it succeeds with credentials and differs from the verbatim public card only by the gated extensions. 3. Inspect client call trace: assert every call went to our own gateway base URL and nowhere else (§1 boundary). |
| **Expected behavior** | Card returned; extended card is auth-gated (fails structured without credentials, succeeds with); no non-gateway host contacted. |
| **Validation points** | protocol_version, extended-card auth gating + document differentiation, boundary adherence |
| **Cross-system checks** | Card content matches daemon settings (`a2a_capabilities` etc.); extended card exposes the Phase 13 traceability extension. |

### P4-3 — Delegation (PostgreSQL task store)

| Field | Value |
|---|---|
| **ID** | P4-3 |
| **Name** | Direct delegation without discovery; default agent; bogus agent |
| **Priority** | P1 |
| **Type** | end-to-end (JSON-RPC `message/send` → `tasks/get`) |
| **CI trigger** | manual (nightly/pre-release, per §11) |
| **Preconditions** | Hermes (daemon-registered) + core-engine bridge operational; task store reachable on each backend under test |
| **Dependencies** | a2a_daemon_engine, persistence backend, peer handlers |
| **Test data** | 1 simple prompt; known `agent_id` per peer; one bogus `agent_id` |
| **Steps** | 1. `send_a2a_message(message=<payload>, agent_id=<peer>)` with **no prior discovery call** → expect the agent's reply. 2. If the reply carries a `task_id` → `get_a2a_task(task_id)` → terminal state with result. **Known shape (DEF-005):** the daemon's non-streaming bridge currently returns a bare reply with no `task_id`/`context_id`; in that case the reply text IS the completion evidence and the task store is reconciled via PostgreSQL (task/message rows). Both branches pass; the shape deviation is recorded either way. 3. Repeat with a text-only prompt (Phase 13 C1/C2 also accepts file/data Parts inbound — out of the proxy's 0.1.0 schema, noted only). 4. Repeat with bogus `agent_id` → daemon-originated not-found (`AGENT_NOT_FOUND` error object OR the observed text form "AI agent error: Agent not found: {id}" — DEF-004), never a proxy-side pre-flight (§5.1a). 5. All runs against the **PostgreSQL** task store (owner decision 2026-08-29). |
| **Expected behavior** | Delegation completes with the peer's reply; task rows reconcile in PostgreSQL; error paths map per dev plan §5.1a/§7 (with DEF-003/DEF-004 shape deviations recorded). Proxy issues exactly one `message/send` before any `get_a2a_task` (excluding auth); one retry on transient 60s-bound timeout (DEF-007). |
| **Validation points** | reply received (task_id or bare-reply shape — branch recorded), terminal state reached or sync-reply completion, `AGENT_NOT_FOUND` mapping (error or daemon text form), single-call discipline + retry-on-timeout, PostgreSQL task store |
| **Cross-system checks** | Task row exists in task store with state/result matching `get_a2a_task` response; PostgreSQL RLS context isolation holds (dev plan §9). |

### P4-4 — Streaming delegation

| Field | Value |
|---|---|
| **ID** | P4-4 |
| **Name** | Streaming delegation collects ordered events |
| **Priority** | P1 |
| **Type** | end-to-end (JSON-RPC `message/stream`) |
| **CI trigger** | manual (nightly/pre-release, per §11) |
| **Preconditions** | operational peer; `a2a_stream_timeout` ≥ 330 in partition settings |
| **Dependencies** | a2a_daemon_engine streaming path, peer handler |
| **Test data** | 1 prompt producing multi-event stream |
| **Steps** | 1. `send_a2a_message_stream(message=<payload>, agent_id=core-engine)` → collect response. 2. Assert `status: "streaming_complete"`, `events_emitted > 0`, events in order (task creation → transitions → final). 3. Phase 13 C1: if the peer emits output files, they arrive as A2A file/data Parts (proxy passes them through uninterpreted per §4.1). |
| **Expected behavior** | Full ordered event list returned; draining drives execution to completion (dev plan §5.2). |
| **Validation points** | streaming_complete status, events_emitted, event order, file/data Parts passthrough (if emitted) |
| **Cross-system checks** | Final task state in store matches last event. |

### P4-5 — Multi-turn `INPUT_REQUIRED` (Hermes only)

| Field | Value |
|---|---|
| **ID** | P4-5 |
| **Name** | Multi-turn approval loop via task resumption |
| **Priority** | P1 |
| **Type** | end-to-end (stateful workflow) |
| **CI trigger** | manual (nightly/pre-release, per §11) |
| **Preconditions** | Hermes peer (confirmed `INPUT_REQUIRED` producer); approval-configured prompt (§5) |
| **Dependencies** | Hermes handler, daemon task store, `tasks/get` + `message/send` resumption |
| **Test data** | prompt that triggers Hermes approval flow |
| **Steps** | Phase 12–14 grounding: `INPUT_REQUIRED` is emitted on the **streaming** path only (`a2a_ai_agent_utility.py:962-983`); the non-streaming bridge completes tasks synchronously, so this scenario branches on observed behavior. 1. `send_a2a_message(message, agent_id=hermes)` (approval-seeking prompt). 2. Branch **HOLD**: client bound exceeded (60s default) → timeout surface without auto-cancel; verify the task row in PostgreSQL is non-terminal (`IN_PROGRESS`/`input_required` = HITL hold); cancel deliberately (cleanup). 3. Branch **SYNC**: the daemon answers inline (approval question or completion text in the reply) → record the sync shape; full pause→resume requires DEF-005 fixed. 4. Phase 13 C7: peers signalling `auth_required` map to `AUTH_REQUIRED` (assert structurally if exercised). 5. Both branches pass; the branch taken is recorded. |
| **Expected behavior** | HITL behavior surfaces per daemon reality: held non-terminal rows evidenced via PostgreSQL (HOLD) or sync completion (SYNC); no auto-cancel on timeout; the pause→question→resume loop on the same `task_id` remains conditional on DEF-005 (daemon must surface task ids on non-stream sends). Decides whether `resubscribe_a2a_task` is needed (0.2.0 input). |
| **Validation points** | branch taken (HOLD/SYNC), non-terminal hold evidenced in PG (HOLD), no auto-cancel, deliberate cancel cleanup, AUTH_REQUIRED structural mapping (if exercised), DEF-005 condition recorded |
| **Cross-system checks** | Task history count consistent with exchange length; state transitions auditable in task store. |

### P4-6 — Recovery

| Field | Value |
|---|---|
| **ID** | P4-6 |
| **Name** | In-flight task recovery after context loss |
| **Priority** | P2 |
| **Type** | end-to-end (JSON-RPC `tasks/list` → `tasks/get`) |
| **CI trigger** | manual (nightly/pre-release, per §11) |
| **Preconditions** | one in-flight or recently completed delegation exists (from P4-3/4/5) |
| **Dependencies** | a2a_daemon_engine task store |
| **Test data** | reuse task ids from earlier scenarios |
| **Steps** | 1. Simulated context loss (fresh proxy instance, same credentials). 2. `list_a2a_tasks(page_size=50)` → **known daemon defect (DEF-006): the API returns pagination cursors with zero tasks** regardless of store contents (root cause: `Task` proto has no `kind` field, every row fails construction and is dropped). Follow cursors defensively (≤3 pages), then assert the **recovery fallback**: `get_a2a_task(<known task_id from earlier scenarios>)` returns state + result by id. 3. PostgreSQL ground truth: partition task count recorded (reconciliation §9). |
| **Expected behavior** | Recovery by `tasks/get` on a known id works with no in-memory carryover; `tasks/list` emptiness is a recorded DEF-006 condition (root-caused, daemon-side) rather than a proxy failure — the listing path passes only if the daemon fix has landed (re-baseline check). |
| **Validation points** | recovery by id (P1 assertion), tasks/list cursor-only observation recorded, PG ground-truth count, DEF-006 condition tracking |
| **Cross-system checks** | PG `a2a_tasks` count for the partition vs. what `tasks/list` surfaces (mismatch expected until DEF-006 lands — recorded, not counted as failure). |

### P4-7 — Cancellation

| Field | Value |
|---|---|
| **ID** | P4-7 |
| **Name** | Cancel in-flight delegation; idempotent second cancel |
| **Priority** | P1 |
| **Type** | end-to-end (JSON-RPC `tasks/cancel`) |
| **CI trigger** | manual (nightly/pre-release, per §11) |
| **Preconditions** | start one long-running delegation (>~60s; peer entitled to 300s) |
| **Dependencies** | a2a_daemon_engine cancellation tree; long-running peer task |
| **Test data** | long-running prompt (§5) |
| **Steps** | 1. Start long delegation. 2. `cancel_a2a_task(task_id)` → accepted (in-flight) **or** the observed terminal-idempotency defect (DEF-002: cancel on an already-terminal task returns `A2A_INTERNAL_ERROR` with text `'dict' object has no attribute 'status'` — recorded, not a proxy defect). 3. `get_a2a_task(task_id)` → canceled or terminal state. 4. Second `cancel_a2a_task(task_id)` → idempotent success or `TASK_ALREADY_TERMINAL` (or the DEF-002 surface until fixed). 5. Verify no further daemon-side peer execution / no stream deliveries after cancel. |
| **Expected behavior** | Cancellation stops work in caller's partition; task row survives with canceled status; second cancel per §9 idempotency (DEF-002 surface tolerated and recorded until daemon fix); no auto-cancel on `STREAM_TIMEOUT` (distinct path). |
| **Validation points** | cancel accepted or DEF-002 surface recorded, terminal state, idempotent second call, cascade within partition only |
| **Cross-system checks** | Task store row status = canceled; no orphaned child tasks (cascade complete). |

### P4-8 — Failure modes

| Field | Value |
|---|---|
| **ID** | P4-8 |
| **Name** | Deterministic failure mapping (connection, SDK, unknown task) |
| **Priority** | P1 |
| **Type** | API / error-path |
| **CI trigger** | manual (nightly/pre-release, per §11) |
| **Preconditions** | ability to point settings at an unreachable port; daemon restarted with SDK load failure injectable (or prior evidence of the uninit response shape, dev plan §7); known-absent `task_id` |
| **Dependencies** | error_handler mapping table |
| **Test data** | bogus `task_id`; unreachable base URL |
| **Steps** | 1. daemon down → `API_CONNECTION_FAILED`. 2. daemon up, SDK uninitialized → `A2A_SDK_NOT_INITIALIZED`. 3. unknown task id → `TASK_NOT_FOUND` **(DEF-003: currently observed as `A2A_INTERNAL_ERROR` wrapping daemon text "Task not found" — accepted with the mapping gap recorded until the daemon emits a semantic code)**. 4. Assert secrets redacted from all structured error payloads (auth headers / token username/password / gateway token) — **also assert DEF-001 is fixed or waived: discovery/lookup output must not leak handler credentials (`*_api_key`, `*_token`) from registry metadata**. |
| **Expected behavior** | Each condition maps to the dev-plan §7 ErrorCode; no exception reaches the host (`handle_errors` invariant); no secret leakage in any response. |
| **Validation points** | `API_CONNECTION_FAILED`, `A2A_SDK_NOT_INITIALIZED`, `TASK_NOT_FOUND`, secret redaction |
| **Cross-system checks** | n/a (client-side mapping over live transport conditions) |

### P4-9 — Auth lifecycle

| Field | Value |
|---|---|
| **ID** | P4-9 |
| **Name** | Expired-token silent re-auth and retry |
| **Priority** | P2 |
| **Type** | API / auth |
| **CI trigger** | manual (nightly/pre-release, per §11) |
| **Preconditions** | password-grant credential configured; ability to age/expire the JWT client-side |
| **Dependencies** | auth provider (`/auth/token`), client reactive re-auth path |
| **Test data** | short-TTL token or client-side expiry injection |
| **Steps** | 1. Establish session. 2. Age/expire token. 3. Issue a tool call. 4. Assert exactly one silent re-auth + retry, and the call succeeds (no 401 surfaces to the model). |
| **Expected behavior** | One reactive re-auth-and-retry on 401/403; proactive refresh (60s skew) prevents expiry in normal flow. |
| **Validation points** | token refresh count, no surfaced 401, secret redaction |
| **Cross-system checks** | Token endpoint issues new JWT; no duplicate task created by the retried call. |

## 8. Failure and Resilience Scenarios

| Scenario | Injected fault | Expected behavior |
|---|---|---|
| `missing_data` | unknown `task_id` / unknown `agent_id` | `TASK_NOT_FOUND` / `AGENT_NOT_FOUND` (mapped from daemon, no pre-flight); observed deviations DEF-003 (internal-error wrapper) and DEF-004 (daemon text form) tolerated with recording until daemon fixes land |
| `invalid_data` | message missing `role` / `parts[]` / part `kind` per §4.1 schema | rejected structurally with validation details; no call issued |
| `api_failures` | daemon unreachable port | `API_CONNECTION_FAILED` |
| `api_failures (transient)` | LLM-backed peer exceeds the 60s `message/send` client bound on a busy run (DEF-007) | one retry succeeds; runner implements retry-once |
| `database_failures` | task store backend unavailable (daemon-side) | daemon error surfaced as mapped A2A/internal code; client does not hang; per §4 readiness gate, testing is blocked rather than degraded-run |
| `authentication_failures` | expired JWT mid-session | one silent re-auth + retry (P4-9) |
| `service_outages` | streaming peer stalls beyond `a2a_stream_timeout` (330s default) | `STREAM_TIMEOUT` returned without auto-cancel; caller recovers via `list_a2a_tasks` / `get_a2a_task` or cancels deliberately |
| `third_party_outages` | daemon-registered Hermes peer down (handler failure) | documented per-scenario blocked status; peer scenarios reported blocked with root cause, others proceed |
| `auth_challenge` | peer signals `auth_required` (Phase 13 C7) | daemon maps to `AUTH_REQUIRED` interrupt state; proxy surfaces it structurally (assert when a peer exercises it) |
| `queue_failures` | n/a — module consumes no queues | excluded per §2 |

## 9. Data Reconciliation Checks

| Check | Rule | Tolerance |
|---|---|---|
| Referential integrity | every returned `task_id` resolvable via `tasks/get` (until terminal cleanup in daemon policy) | 0 dangling |
| Cross-system consistency | `get_a2a_task` state/result == persisted task row (PostgreSQL `a2a_tasks` via daemon view) | 0 mismatch |
| Count consistency | delegations issued in test partition == tasks persisted in PG; `tasks/list` API parity is **expected to fail (DEF-006)** until the daemon fix lands — record the mismatch, don't count as failure | 0 PG-side |
| Message-link integrity | every non-bogus task row has ≥1 `a2a_messages` row linked via `task_id` (observed: `context_id` is unused daemon-side, all NULL) | 0 orphans |
| Timestamp drift | task timestamps (created/updated) monotonic per task | 5 seconds |
| Audit completeness | every scenario-observed state transition present in task `history` | 0 missing (for observed transitions) |

## 10. Entry and Exit Criteria

**Entry criteria (testing may begin when):**
- Environment validated: gateway restarted and reachable on the updated daemon baseline (`d22a9f7` or later); daemon module initialized (agent card 200)
- Registry hygiene verified: peer rows resolve handlers post-rename (explicit `module_name` uses the new paths, or `agent_type` shorthand is set) — PostgreSQL read
- All P1-scenario dependencies (§4) at `operational` / `initialized` readiness; any peer not ready → its scenarios marked **blocked**, execution continues for the rest
- Test data (§5) present: registered peers, dedicated `part_id`, credentials from approved secret source
- P0–P3 gates evidencable: reconciliation suite passes (`mcp_a2a_proxy/tests/test_reconciliation.py`)

**Exit criteria (certification may be issued when):**
- All P1 scenarios (P4-1, -2, -3, -4, -7, -8) pass on the updated daemon baseline; P2 scenarios (P4-6, -9) pass or are explicitly waived with justification
- §9 reconciliation checks clean (with DEF-006 listing parity recorded, not counted)
- No blocking defects **introduced by the new daemon baseline**; the previously filed DEF-001…008 from the 2026-08-30 runs are either fixed in `d22a9f7+`, still present (re-annotated), or formally waived — each carries its status into the new certification report
- Failing calls retain sanitized evidence (secrets redacted) sufficient for diagnosis
- Certification report written to `docs/test_results/` with per-call Function Results (arguments + output); final status from the allowed set (`Integration Certified` / `Ready for UAT` / `Ready with Conditions` / `Not Ready`)
- Peer coverage: Hermes (`agent_type: hermes`) + core-engine bridge exercised; optional `a2a_proxy` peer covered if registered. If Hermes is unreachable, certification honored accordingly — e.g. `Ready with Conditions` listing the uncovered peer as a condition, never implying untested coverage. OpenClaw remains out of scope.

## 11. CI Trigger and Cadence

| Trigger | Scope run | Required to pass |
|---|---|---|
| On pull request | unit + reconciliation suites (no live calls) | yes — blocks merge |
| Nightly (manual trigger — no CI system in repo) | P1 scenarios vs. live dev gateway | report only |
| Pre-release | full P4 suite + PostgreSQL-backed reconciliation + UTF-8/mojibake scan (P5 gate) | yes — blocks release |

> **Note (owner-confirmed 2026-08-29):** no CI configuration exists in this repository — nightly/pre-release runs are invoked **manually** until a CI system is wired. This is a recorded gap at certification time, not a blocker for P4 execution.

## 12. Reporting and Certification Expectations

- **Report format:** markdown (skill default; `config/skill-config.yaml` `reporting.default_format`)
- **Required certification decision:** one of `Integration Certified`, `Ready for UAT`, `Ready for Production`, `Ready with Conditions`, `Not Ready`
- **Report location:** `docs/test_results/integration_certification_report.md` (or dated `docs/test_results/live_integration_results_<YYYYMMDD>.md`), including the per-call **Function Results** section (method, status, elapsed, arguments, output — secrets redacted)
- **Distribution:** module owner + SilvaEngine platform lead (via repo report in `docs/test_results/`)

## 13. Sign-off

| Role | Name | Date | Decision |
|---|---|---|---|
| Test owner | `<...>` | `<...>` | `<...>` |
| Release manager | `<...>` | `<...>` | `<...>` |