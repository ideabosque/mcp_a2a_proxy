# MCP A2A Proxy — Architecture and Development Plan

**Package:** `mcp_a2a_proxy`
**Target version:** 0.1.0
**Status:** Implementation in progress — scaffold and all eight tools exist; remaining
unit-contract gaps, live integration and release certification are tracked in §8
**Author:** Idea Bosque
**License:** MIT
**Last updated:** 2026-08-02

## 1. Purpose

`mcp_a2a_proxy` is an **MCP→A2A adapter**: it lets a tool-calling agent act as an A2A
client, communicating with other agents over the A2A protocol on its behalf. The
calling agent gets A2A participation without implementing any of A2A — it just calls
tools.

It is a standard SilvaEngine MCP module, sibling to `mcp_hospirfq_processor` and
`mcp_kg_inquirer`, running on top of `mcp_daemon_engine`.

### It is an A2A client *of the local daemon*, not a direct peer dialler

```text
LLM agent
   │ MCP tools/call
   ▼
mcp_a2a_proxy            ← this module: MCP tool surface, A2A client semantics
   │ HTTP (JSON-RPC / GraphQL / REST)
   ▼
silvaengine_gateway
   │
   ▼
a2a_daemon_engine        ← the A2A server + broker: task store, partitioning, routing
   │ per-agent handler (module_name / class_name)
   ▼
Hermes │ OpenClaw │ ai_agent_core_engine
```

The proxy does **not** open A2A connections to arbitrary remote agents. It always talks
to the local `a2a_daemon_engine` (through `silvaengine_gateway` —
`../silvaengine_gateway`, deployed via `../../docker-silvaengine-gateway`; the daemon
is a registered gateway module, not a standalone service,
`a2a_daemon_engine/README.md:8-19`). The daemon owns the A2A server interface and
brokers to backends through per-agent handler metadata.

That is the right split: the daemon already provides task persistence, multi-tenant
partitioning, handler abstraction, streaming, and cancellation cascade. A proxy that
dialled peers directly would have to reimplement all of it and would lose the shared
task store. The proxy stays thin — it is the daemon's MCP-facing client.

### Why this framing settles the earlier scope questions

Every scope decision in this plan follows from "client, not server, not operator":

| Decision | Because a client… |
|---|---|
| No agent/task/message/setting mutations (§1 scope) | …does not administer the server |
| No capability-based auto-selection (§5.1a) | …addresses a peer; it does not route |
| No pre-flight `agent_id` validation (§5.1a) | …lets the server resolve and report |
| Task tools mirror A2A protocol methods, not DB rows (§5.3) | …speaks the protocol, not the schema |
| Streaming returns collected events (§5.2) | …consumes a response; SSE is the server's channel |

### Scope: client-side only

The caller is an LLM agent working a task, not an operator administering the daemon.

| In scope — a delegating agent | Out of scope — operator work |
|---|---|
| Find a peer agent that can do the job | Registering / editing / deleting agents |
| Inspect peer registry signals and the daemon's protocol capabilities | Managing daemon settings |
| Send it work and collect the result | Writing task / message rows directly |
| Handle a peer that asks a follow-up question | Auditing another tenant's history |
| Abandon work it no longer needs | Push-notification webhook configs |

Admin CRUD is handled by an external tool (confirmed), so the right column stays out
permanently. Including it would triple the tool count and hand the model destructive
capabilities it has no reason to hold.

## 2. Module Shape

Follows the sibling modules exactly:

```text
mcp_a2a_proxy/
├── mcp_a2a_proxy/
│   ├── __init__.py            # exports MCPA2AProxy, MCP_CONFIGURATION
│   ├── mcp_configuration.py   # MCP_CONFIGURATION: tools[] + module_links[]
│   ├── mcp_a2a_proxy.py       # MCPA2AProxy facade (flat mixin composition)
│   ├── a2a_client.py          # endpoint_id/part_id, JSON-RPC + GraphQL + REST calls
│   ├── a2a_backed_processor.py # shared client access and host-facing properties
│   ├── error_handler.py       # ErrorCode, handle_errors, build_error_response
│   ├── discovery_mixin.py
│   ├── delegation_mixin.py
│   ├── task_mixin.py
│   └── tests/
├── docs/
├── pyproject.toml
└── README.md
```

`mcp_kg_inquirer` keeps everything in one `client.py`; `mcp_hospirfq_processor` splits
into mixins. At 8 tools across three domains the split is worth it, but it stays flat
— mixins inherit only from the base and are composed on the facade, as in
`mcp_hospirfq_processor.py:31-57`.

### Host contract

Everything the host requires, and nothing more (`mcp_kg_inquirer/client.py:151-173`):

```python
class MCPA2AProxy:
    def __init__(self, logger: logging.Logger, **setting: Dict[str, Any]): ...

    @property
    def endpoint_id(self) -> str | None: ...
    @endpoint_id.setter
    def endpoint_id(self, value: str): ...

    @property
    def part_id(self) -> str | None: ...
    @part_id.setter
    def part_id(self, value: str): ...

    def a_tool_method(self, **arguments: Dict[str, Any]) -> Dict[str, Any] | str: ...
```

- The host constructs the class with `logger` plus the setting dict, then assigns
  `endpoint_id` and `part_id` derived from the consuming side's `partition_key`.
  **Both properties must exist** or the host skips the assignment.
- `part_id` can be `None`; the `Part-Id` header is then dropped rather than sent as
  `"None"` (the guard in `graphql_client.py:280-286`).
- Tool methods take `**arguments` and return either a dict or a plain sentence for
  expected empty results; the MCP link still declares `return_type: "text"`.
- Settings are stored per partition in the MCP registry, not in this package.

### Method conventions

Every tool method follows the sibling pattern exactly
(`mcp_hospirfq_processor/catalog_mixin.py:14-56`):

```python
@handle_errors(operation_name="discover_a2a_agents")
def discover_a2a_agents(self, **arguments: Dict[str, Any]) -> Dict[str, Any] | str:
    variables = {                                       # camelCase for GraphQL
        "agentName":  arguments.get("agent_name"),
        "status":     arguments.get("status"),          # schema default: "active"
        "pageNumber": arguments.get("page_number"),
        "limit":      arguments.get("limit"),
    }
    variables = {k: v for k, v in variables.items() if v is not None and v != ""}

    result = self._execute_graphql_query(
        "a2a_daemon_engine", "a2aAgentList", "Query", variables
    )
    if error := propagate_error_if_present(result):
        return error

    data = humps.decamelize(convert_decimal_to_number(result))
    if is_empty_result(data.get("a2a_agent_list")):
        return "No A2A agents found matching this query."
    return data
```

Required arguments are checked first, as in `get_a2a_agent`:

```python
agent_id = arguments.get("agent_id")
validate_not_empty(agent_id, "agent_id")
```

Four conventions worth calling out because they are easy to diverge from:

1. **Empty results return a plain sentence, not an error.** `is_empty_result` →
   `"No A2A agents found matching this query."` A model reads that correctly; an
   `error_code` makes it think something broke.
2. **Responses are decamelized** via `humps.decamelize` so the model sees snake_case.
   For JSON-RPC this applies to the *response only* — A2A request field names
   (`contextId`, `taskId`, `messageId`) are protocol, and must be sent verbatim.
3. **Defaults live in the tool's `inputSchema`**, not in method code —
   `"default": "active"`, `"default": 10` (as in `mcp_kg_inquirer/client.py:33-69`).
   The host applies them before the method is called.
4. **Required arguments use `validate_not_empty`**, which raises `ValidationError` and
   is converted to a structured response by `handle_errors`.

`error_handler.py` is ported from `mcp_hospirfq_processor` with the A2A-specific codes
in §7 — `ErrorCode`, `MCPError` subclasses, `handle_errors`, `build_error_response`,
`build_error_from_exception`, `propagate_error_if_present`, `validate_not_empty`,
`is_empty_result`.

## 3. Backend Surface Consumed

### 3.1 JSON-RPC — `POST /{endpoint_id}/a2a` (primary)

Six methods, from `a2a_daemon_engine/main.py:212-349`; five cover delegation and task
management, and one retrieves the optional authenticated daemon card:

| Method | Purpose | Result |
|---|---|---|
| `message/send` | Delegate work | SDK `SendMessageResponse` |
| `message/stream` | Delegate, collecting intermediate events | `{status: "streaming_complete", events_emitted, events[]}` |
| `tasks/get` | Status, result, history | SDK `Task` |
| `tasks/list` | Find own in-flight work | SDK task page |
| `tasks/cancel` | Abandon | SDK `Task` |
| `agent/getAuthenticatedExtendedCard` | Read the authenticated daemon card | SDK agent card |

The daemon accepts aliases for several of these; the proxy always sends the canonical
name above. Non-JSON-RPC payloads are rejected (`main.py:154`); unknown methods return
`-32601`.

Unused: `tasks/pushNotificationConfig/*` (needs a public webhook the agent lacks) and
`tasks/resubscribe` (see §5.3). The extended-card method is exposed through
`get_a2a_agent_card(extended=true)` rather than as a ninth MCP tool.

### 3.2 REST — `GET /{endpoint_id}/.well-known/agent-card.json`

The local daemon's capability document — skills, input/output modes and protocol
version (`main.py:104-116`). It describes the daemon, not an individual registered
peer, and is optional context rather than a required delegation step.

### 3.3 GraphQL — `POST /{endpoint_id}/a2a_core_graphql` (reads only)

Two queries of the ten in `schema.py`:

- `a2aAgentList` — discovery, filtered by `status` / `agent_name`, paged
- `a2aAgent` — one agent's registry record by `agent_id`

Returns `agent_id`, `agent_name`, `capabilities`, `endpoint_url`, `status`,
`metadata`, timestamps. **No mutations** — all eight are operator surface (§1).

## 4. Client Design

One `a2a_client.py`, three call styles over shared auth and error mapping:

| Style | Used by | Endpoint |
|---|---|---|
| JSON-RPC | delegation, task tools | `{gateway_base_url}/{endpoint_id}/a2a` |
| GraphQL | discovery | `{gateway_base_url}/{endpoint_id}/a2a_core_graphql` |
| REST GET | agent card | `{gateway_base_url}/{endpoint_id}/.well-known/agent-card.json` |

**Auth** is ported from `mcp_hospirfq_processor/graphql_client.py:139-183`, unchanged:
best-effort JWT `exp` parse, 60s proactive-refresh skew, `POST {gateway_base_url}/auth/token`
password grant, and one reactive re-auth-and-retry on 401/403. Falls back to
`x-api-key` when no JWT is configured. Headers on every call: `Authorization` or
`x-api-key`, plus `Part-Id` and `Content-Type`, with `None` values dropped.

**GraphQL documents are hand-written** in the client rather than generated via
`Graphql.get_graphql_schema()`. Only two read queries are needed, and generating them
would require importing `a2a_daemon_engine` — pulling `a2a-sdk`, PynamoDB and
SQLAlchemy into this package for no benefit. P4 contract-tests both documents against
the live schema.

**JSON-RPC envelope** is built by the client; `id` is a monotonic counter
(deterministic in tests). Success unwraps `result`; `error` maps to
`build_error_response` so no tool raises. `-32601 → A2A_METHOD_NOT_FOUND`,
`-32602 → A2A_INVALID_PARAMS`, `-32603 → A2A_INTERNAL_ERROR`.

Endpoint URLs are built from configured templates with `{endpoint_id}` quoted as a
single path segment before substitution. `endpoint_id` must not be concatenated raw
into a path. Auth secrets (`Authorization`, `x-api-key`, token username/password and
gateway token) are redacted from logs and from structured error responses.

Timeout policy is explicit: a `message/stream` client timeout returns
`STREAM_TIMEOUT` and does **not** auto-cancel the task. The peer may still be running
inside the daemon. The caller can recover with `list_a2a_tasks` / `get_a2a_task` or
choose to call `cancel_a2a_task` deliberately.

## 4.1 Tool API Contracts

These schemas are the 0.1.0 public tool contract. They must remain synchronized with
`MCP_CONFIGURATION` so a model can form valid A2A payloads without knowing SDK
internals.

### `discover_a2a_agents`

Args: `agent_name?`, `status? = "active"`, `page_number? = 1`, `limit? = 20`.
Returns the decamelized `a2a_agent_list` result. If `capabilities` is a JSON string,
parse it to a list; if parsing fails, keep the raw string.

### `get_a2a_agent`

Args: `agent_id` (required). Returns the decamelized registry record for that peer.
No delegation-time validation depends on this tool.

### `get_a2a_agent_card`

Args: `extended? = false`. Returns this daemon's own card only. It has no `agent_id`
argument in 0.1.0; peer-card fetching is a deferred 0.2.0 design option.

### `send_a2a_message` / `send_a2a_message_stream`

Args: `message` (required), `agent_id?`, `task_id?`, `context_id?`, `metadata?`,
`thread_id?`, `run_id?`. `agent_id` is passed as metadata/state for daemon routing and
is never pre-validated by the proxy.

`message` is an A2A message object and is sent with protocol field names intact:

```json
{
  "role": "user",
  "parts": [
    {
      "kind": "text",
      "text": "Summarize the attached requirements."
    }
  ]
}
```

The 0.1.0 schema accepts `parts[]` objects and requires each part to include `kind`.
At minimum, `kind: "text"` with `text` is supported. Other protocol-native part kinds
may pass through untouched, but the proxy does not reinterpret them.

When `task_id` and/or `context_id` are supplied, they are sent as `taskId` and
`contextId` so a follow-up can resume an existing `input_required` task.

### `get_a2a_task`

Args: `task_id` (required), `history_length?`. Returns current task state, result,
artifacts and requested history. `input_required` is non-terminal.

### `list_a2a_tasks`

Args: `page_size?`, `page_token?`, `status?`, `priority?`, `task_type?`, and
`assigned_agent_id?`. The proxy maps the last two to protocol fields `type` and
`assignedAgentId`. This is the recovery tool after context loss or a stream timeout.

### `cancel_a2a_task`

Args: `task_id` (required). Cancels an in-flight task in the caller's partition and
may cascade to child tasks through the daemon cancellation tree.

## 5. Tool Surface — 8 tools

One loop: **discover → delegate → track → abandon.**

### 5.1 `DiscoveryMixin` (3)

| Tool | Call | Purpose |
|---|---|---|
| `discover_a2a_agents` | GraphQL `a2aAgentList` | Find peers — *who exists*. Args: `agent_name?`, `status?` (schema default `active`), `page_number?` (default 1), `limit?` (default 20) |
| `get_a2a_agent` | GraphQL `a2aAgent` | One peer's registry record. Args: `agent_id` (required) |
| `get_a2a_agent_card` | REST, or JSON-RPC when `extended=true` | This daemon's own card — skills, input/output modes, protocol version. Args: `extended?` (default `false`). No `agent_id`: the card is daemon-level, and fetching remote peers' cards is deferred (§5.1a) |

**`capabilities` is unreliable, and no authoritative per-peer capability source exists
in 0.1.0.** Two separate problems with the registry field:

1. *It is usually empty.* The reference Hermes registration
   (`tests/register_hermes_agent.py:109-118`) omits the column entirely from its
   `INSERT`, and it is `nullable=True` (`models/postgresql/a2a_agent.py:40`). In the
   one confirmed deployment it is **NULL**.
2. *When set, it arrives as a JSON string.* Stored as
   `UnicodeAttribute()  # JSON string of list` and written with
   `Serializer.json_dumps(...)` (`models/dynamodb/a2a_agent.py:58, 221`); the read path
   never reverses it (`a2a_agent.py:140-147`), so the GraphQL `String` returns
   `'["chat","streaming"]'`.

So the discovery tools `json.loads` it when present, falling back to the raw string.

**And the agent card does not substitute for it.** The card is built once from
*server-level* settings — `self.settings.get("a2a_capabilities", [...])`
(`a2a_server.py:282-293`) — and `agent_card()` returns that single
`Config.a2a_server.agent_card` with only the endpoint URL substituted per request
(`main.py:374-386`). There is no `agent_id` parameter on the card route or on the
extended card (`a2a_extended_card.py:241`, `328-350`). **One card per daemon,
describing the daemon itself — not one card per registered agent.**

This constrains what capability data exists per peer (§5.1a).

**Discovery results are cached.** `resolve_a2a_agent_list` is wrapped in
`method_cache(ttl=Config.get_cache_ttl())` (`queries/a2a_agent.py:22`), so a
newly-registered peer may not appear immediately. Noted in the tool description; a
model that just registered a peer should not treat an empty list as proof of absence.

**`agent_name` is a substring match** (`A2AAgentModel.agent_name.contains(...)`), and
`status` filtering runs through the `status-index` GSI rather than a scan
(`a2a_agent.py:179-182`) — cheap, so filtered discovery is the preferred path.

### 5.1a Delegation does no agent lookup

**`agent_id` is optional, and when supplied the proxy passes it straight through — no
resolution, no validation, no enrichment.** Delegation is a single call.

```text
send_a2a_message(agent_id="hermes-agent", message=…)   -> straight to that agent
send_a2a_message(message=…)                            -> daemon's default agent
```

Both cases are already handled one layer down: `resolve_agent` does
`agent_id = agent_uuid or _default_agent_uuid()` (`a2a_ai_agent_utility.py:114-135`),
and an unknown id comes back as `"Agent not found: {id}"`
(`a2a_ai_agent_utility.py:803-807`), which maps to `AGENT_NOT_FOUND`.

So the proxy **must not** pre-flight the agent id. A validating lookup before every
delegation would add a round-trip, duplicate resolution the daemon already performs,
and create a second place where "which agent?" is decided.

**Discovery and the agent card are optional aids, not pipeline stages.** A caller that
already knows the id — from a prior turn, configuration or its own instructions —
calls `send_a2a_message` directly. `discover_a2a_agents` helps when the caller does not
know the id. `get_a2a_agent_card` provides daemon-level protocol context; it does not
identify or validate a peer.

#### Why the proxy does not auto-select

A single `delegate_task(description)` tool that picks a peer itself was considered and
rejected:

1. **The data to match on is not there.** Registry `capabilities` is NULL in the one
   confirmed deployment, and the card is daemon-level (§5.1).
2. **Matching intent to a capability is reasoning, not lookup.** The caller is already
   an LLM; a substring match in the proxy would be brittle, and an LLM call inside the
   proxy would be a second model invocation doing what the first one could.
3. **Delegation should be legible.** If the proxy picks silently, the calling agent
   cannot explain or override the choice, and a bad pick is invisible in the trace.
4. **The default already covers indifference** — omit `agent_id` and the daemon routes.

#### What capability data actually exists per peer

Since the card is daemon-level (§5.1), the per-agent signals are thinner than the A2A
protocol suggests:

| Registry field | Usable for selection? |
|---|---|
| `agent_name` | **Yes** — populated and legible (`"Hermes Agent"`) |
| `metadata.module_name` / `class_name` | **Indirectly** — identifies the backend (Hermes, OpenClaw, core engine) |
| `capabilities` | **No** — NULL in the reference registration |
| `endpoint_url` | **Only if genuinely remote** — see below |
| `status` | Filter only |

A **remote** peer runs its own A2A daemon and therefore has its own card at
`{endpoint_url}/.well-known/agent-card.json`. Hermes and OpenClaw are separately
deployed gateways (§12 Q1), so registering them with their real URLs makes that card
fetch meaningful. A **locally registered** agent routes through an in-process handler
and is not an independent A2A server — the reference registration sets
`endpoint_url = http://127.0.0.1:8765`, our own gateway, so fetching its "card" returns
our own.

#### Deferred decision: should the proxy fetch a *remote* peer's card?

Fetching `{endpoint_url}/.well-known/agent-card.json` for a remote peer is the only
place the proxy would talk to something other than its own daemon — it breaks the
boundary drawn in §1.

**Recommendation: don't, in 0.1.0.** `get_a2a_agent_card` takes no `agent_id` and
returns this daemon's own card. Reasons:

- It preserves the clean rule "the proxy only talks to its own daemon," which is what
  makes this module thin and its failure modes few.
- It needs network reachability from the MCP gateway to every peer gateway — a
  deployment burden for a discovery nicety.
- The payoff is small: it only helps the *find an agent I don't already know* path,
  which is not the common case (§5.1a), and `capabilities` on the registry record is
  the cheaper fix for that.
- Locally-handled agents have no distinct card anyway, so the feature would work for
  some registry entries and not others — a confusing tool contract.

Revisit if genuinely remote peers become the norm rather than the exception. The
alternative design — optional `agent_id`, fetch the peer's card when `endpoint_url`
points off-box, report "locally handled, no distinct card" otherwise — is recorded here
so it can be picked up without re-deriving it.

When a caller *does* need to find an id first, discovery is the peer-specific aid.
The card is optional daemon-level context, not a peer capability lookup:

```text
discover_a2a_agents()              -> who exists (name, status, backend metadata)
get_a2a_agent_card()               -> this daemon's own card, not a peer-specific card
send_a2a_message(agent_id=…, …)    -> delegate
```

**Ops recommendation, outside this module:** populate `capabilities` at registration
time. It is the one field designed for this, it is already read and parsed by
`discover_a2a_agents`, and filling it would make the discovery step a single cheap
indexed query instead of per-peer card fetches. Until then, discovery rests on
`agent_name` plus backend metadata.

### 5.2 `DelegationMixin` (2)

| Tool | Method | Notes |
|---|---|---|
| `send_a2a_message` | `message/send` | Delegate. Args: `message` (role + parts), `agent_id?` (passed through, no lookup — omit for the daemon default, §5.1a), `task_id?`, `context_id?`, `metadata?`. Also the follow-up call in a multi-turn exchange (§5.4) |
| `send_a2a_message_stream` | `message/stream` | Delegate, returning the full ordered `events[]` — task creation, status transitions, artifact updates, final message. Bounded by `a2a_stream_timeout` |

`send_a2a_message_stream` returns the *collected* event list, not incremental output:
`_collect_message_stream` (`main.py:500-519`) drains the SDK generator and serializes
every event. Draining it is what drives `AgentExecutor.execute()` to completion, so
this is the same execution path SSE subscribers observe, not a degraded one. For a
delegating agent that is the right shape — it reasons over the complete sequence
anyway. Live per-token output remains available to a UI on the gateway's
`GET /{ep}/sse`, which this package neither owns nor interferes with.

`agent_id` / `thread_id` / `run_id` are promoted by the daemon into
`ServerCallContext.state` as `agent_uuid` / `thread_uuid` / `run_uuid`
(`main.py:194-207`) — documented in the schemas so the model knows the routing knob.

### 5.3 `TaskMixin` (3)

| Tool | Method | Notes |
|---|---|---|
| `get_a2a_task` | `tasks/get` | Status, result, and — with `history_length` — the peer's question when it is waiting on input (§5.4). Args: `task_id`, `history_length?` |
| `list_a2a_tasks` | `tasks/list` | Recover in-flight delegations after context loss. Args: `page_size?`, `page_token?`, filters |
| `cancel_a2a_task` | `tasks/cancel` | Abandon work. Args: `task_id`. **Cascades to child tasks** (`a2a_cancellation.py:56-111` cancels the delegation subtree and notifies each agent) — the description must say so |

`resubscribe_a2a_task` is **deferred to 0.2.0**. `tasks/resubscribe` returns the event
stream; `tasks/get` returns current state plus history. For every recovery case an
agent has — context compaction, restart — state and the last message are what it
needs, and `get_a2a_task` supplies both. Ordered intermediate events matter to a UI,
not to a delegating agent. Revisit if P4 shows an agent unable to reconstruct a
multi-turn exchange from `history_length` alone.

`cancel_a2a_task` **ships in 0.1.0** despite being the only destructive tool. Its blast
radius is categorically smaller than the deletes cut in §1: it stops in-flight work in
the caller's own partition, task rows survive with a cancelled status, and the effect
is reversible by re-delegating. Omitting it leaves an agent that has changed plan with
no way to stop a peer burning tokens.

### 5.4 Multi-turn (`INPUT_REQUIRED`)

A peer can pause mid-task to ask for input or human approval — actively used by the
Hermes approval flow (`a2a_ai_agent_utility.py:1026-1029`,
`a2a_server.py:470-475`). No extra tool is needed; the loop composes:

```text
send_a2a_message(message, agent_id)          -> task_id, state = input_required
get_a2a_task(task_id, history_length=N)      -> read the peer's question
send_a2a_message(message=answer, task_id=…)  -> resumes the SAME task
get_a2a_task(task_id)                        -> completed + result
```

Two implementation requirements, both easy to miss:

- `send_a2a_message` must pass `task_id` / `context_id` through untouched, so the
  follow-up resumes the existing task instead of starting a new one.
- Both descriptions must name `input_required` as a **non-terminal** state and point at
  each other. A model that reads it as failure will abandon a task that is merely
  waiting on it.

### 5.5 Totals

| Area | Tools |
|---|---:|
| Discovery | 3 |
| Delegation | 2 |
| Task tracking | 3 |
| **Total** | **8** |

Each tool gets one `module_links` entry: `type: "tool"`,
`module_name: "mcp_a2a_proxy"`, `class_name: "MCPA2AProxy"`, `return_type: "text"`.

## 6. Settings

Stored per partition in the MCP registry and passed to `__init__` as `**setting`:

```python
{
    "gateway_base_url": "http://localhost:8765",
    "a2a_jsonrpc_endpoint": "http://localhost:8765/{endpoint_id}/a2a",
    "a2a_agent_card_endpoint":
        "http://localhost:8765/{endpoint_id}/.well-known/agent-card.json",
    "graphql_modules": {
        "a2a_daemon_engine": {
            "endpoint": "http://localhost:8765/{endpoint_id}/a2a_core_graphql",
            # one of: x_api_key | (token_username + token_password) | gateway_token
            "gateway_base_url": "http://localhost:8765",
            "token_username": "svc",
            "token_password": "replace-me",
        }
    },
    # 300s (the Hermes/OpenClaw handler default) + 30s margin. NOT 120 —
    # see §6.1; A2A_STREAM_TIMEOUT=120.0 in the gateway .env bounds only the
    # core-engine bridge, not the peer handlers this module delegates to.
    "a2a_stream_timeout": 330,
    "default_page_limit": 20,
}
```

### 6.1 Why `a2a_stream_timeout` is 330, not 120

The proxy waits on the daemon, which waits on the peer's handler. The handler defaults
are what actually bound a delegation:

| Peer handler | Setting | Default | Deployment |
|---|---|---|---|
| `HermesAgentHandler` | `hermes_timeout` / `HERMES_STREAM_TIMEOUT` | **300.0s** | `docker-a2a-hermes-agent-gateway/.env:83` |
| OpenClaw handler | `openclaw_timeout` / `OPENCLAW_STREAM_TIMEOUT` | **300.0s** | `docker-a2a-openclaw-gateway/.env:89` |
| Core engine bridge | `core_engine_stream_timeout` / `CORE_ENGINE_STREAM_TIMEOUT` | 120.0s | in-house |

`A2A_STREAM_TIMEOUT=120.0` in the gateway `.env` corresponds to the **core-engine
bridge only**. Setting the proxy's client timeout to 120 would cut off a Hermes or
OpenClaw peer that is legitimately entitled to 300s — and would produce the worst
failure mode available: the agent receives `STREAM_TIMEOUT` while the peer keeps
running, so the work is orphaned *and* the error is wrong.

The client timeout must therefore exceed the slowest peer: **330s** (300 + 30s margin).
Per-partition settings can lower it where only the core-engine bridge is in play.

This is the one place the sync-method decision (§12) has a real cost — a delegation can
block for up to 330s. P4 measures actual peer durations; if the p99 is far below 300,
lowering the ceiling is safe and preferable.

`{endpoint_id}` is substituted at call time; `part_id` goes in the `Part-Id` header.
Because the record is per-partition, tenants can point at different gateways and
credentials with no code change. Store keys snake_cased — the host normalizes, and
matching avoids surprises. No AWS environment variables are read.

## 7. Error Handling

`handle_errors` on every tool method — no exception reaches the host.

```
# Transport
API_CONNECTION_FAILED, AUTH_FAILED, GRAPHQL_QUERY_FAILED
# JSON-RPC
A2A_METHOD_NOT_FOUND (-32601), A2A_INVALID_PARAMS (-32602),
A2A_INTERNAL_ERROR (-32603), A2A_SDK_NOT_INITIALIZED
# Domain
AGENT_NOT_FOUND, TASK_NOT_FOUND, TASK_ALREADY_TERMINAL,
AGENT_CARD_UNAVAILABLE, STREAM_TIMEOUT
```

Two backend responses need explicit mapping because they are not HTTP errors:

- `{"jsonrpc":"2.0","error":{"code":-32603,"message":"A2A SDK not initialized"}}`
  (`main.py:157-164`) → `A2A_SDK_NOT_INITIALIZED`, hinting the daemon module failed to
  load in the gateway.
- `agent_card` returning `{"error": "A2A SDK not initialized", ...}`
  (`main.py:378-382`) → `AGENT_CARD_UNAVAILABLE`.

## 8. Phases

Current snapshot (verified 2026-08-02): the package, eight tool definitions and 59 unit
tests are present, and the unit suite passes. That evidence does not close phase gates
whose listed assertions or live artifacts are still missing.

| Phase | Status | Deliverable | Exit criteria |
|---|---|---|---|
| **P0 — Scaffold** | **Complete** | `pyproject.toml` (`pyhumps`, `httpx[http2]`, `silvaengine-utility`), `__init__.py`, `error_handler.py`, `a2a_backed_processor.py` with `endpoint_id` / `part_id` | `pip install -e .[dev]`; `compileall` clean |
| **P1 — Client** | **In progress** | `a2a_client.py` — auth, JSON-RPC, GraphQL documents, REST | `httpx.MockTransport` tests must cover token issuance, proactive refresh, reactive 401 retry, `x-api-key` fallback, optional `Part-Id`, endpoint quoting, secret redaction, malformed responses, stream timeout → `STREAM_TIMEOUT`, non-stream timeout → `API_CONNECTION_FAILED`, GraphQL `errors`, and JSON-RPC error mapping. **Gap:** add explicit token-issuance and proactive-refresh assertions. |
| **P2 — Tools** | **In progress** | `discovery_mixin.py`, `delegation_mixin.py`, `task_mixin.py` (8 tools) | Tests must assert canonical methods, required arguments, §4.1 schemas, empty-result sentences, capability parsing, decamelized responses and verbatim A2A request fields. **Gap:** make `message.role`, `message.parts[]`, and each part's `kind` structural schema requirements, then test invalid payload rejection. |
| **P3 — Facade + config** | **Complete** | `mcp_a2a_proxy.py`, `mcp_configuration.py` | Reconciliation proves 8 unique tools, 8 unique links, a tool/link bijection, resolvable `function_name` values, and both host properties on the facade. |
| **P4 — Live integration** | **Pending** | `mcp_a2a_proxy/tests/run_integration.py`, `docs/integration_scenarios_sop.md` | §9 passes against the running gateway and required peers on both persistence backends; failures retain enough sanitized evidence to diagnose. |
| **P5 — Docs + release** | **In progress** | `README.md`, version pin, certification report under `docs/test_results/` | README and version agree with the shipped API; all P4 gates pass; UTF-8/mojibake scan is clean; the report records tested component versions, backends, scenarios, known limitations and release decision. **Gap:** normalize corrupted punctuation currently present in Python docstrings and tool descriptions. |

## 9. Live Integration Scenarios (P4)

Against `silvaengine_gateway` with `a2a_daemon_engine` registered.

Run scenarios 1–4 against **both** deployed peers (Hermes and OpenClaw); run scenario 5
against **Hermes**, the confirmed `INPUT_REQUIRED` producer (§12 Q1).

1. **Discovery** — `discover_a2a_agents` returns registered peers; `status` filter
   works; populated `capabilities` arrives as a parsed **list**, while NULL remains
   absent rather than becoming an empty list (§5.1); a
   wrong `part_id` returns nothing (tenant isolation holds).
2. **Capability read** — `get_a2a_agent_card()` returns this daemon's card with protocol
   version `1.0.0` and a gateway-substituted endpoint URL; `extended=true` respects auth
   gating. Assert the call goes to our own gateway and nowhere else (§1 boundary).
3. **Delegation** — `send_a2a_message` with a known `agent_id` and **no prior discovery
   call** → `task_id` → `get_a2a_task` reaches a terminal state carrying the result.
   Assert the proxy issues exactly one A2A JSON-RPC `message/send` call before
   `get_a2a_task`, excluding auth, with no pre-flight agent lookup (§5.1a).
   Repeat omitting `agent_id` and confirm the daemon's default agent handles
   it; then with a bogus `agent_id` and confirm `AGENT_NOT_FOUND` comes from the
   daemon rather than a proxy-side check.
4. **Streaming delegation** — `send_a2a_message_stream` returns
   `status: "streaming_complete"` with `events_emitted > 0` and events in order.
5. **Multi-turn** (§5.4) — delegate to a peer requiring approval; assert the task
   reaches `input_required` rather than a terminal state, `get_a2a_task(history_length=N)`
   surfaces the question, `send_a2a_message` with the same `task_id` resumes the *same*
   task, and the exchange completes. Decides whether `resubscribe_a2a_task` is needed.
6. **Recovery** — `list_a2a_tasks` finds an in-flight delegation after simulated
   context loss; `get_a2a_task` retrieves its result.
7. **Cancellation** — `cancel_a2a_task` on an in-flight task; a second cancel is
   idempotent or returns `TASK_ALREADY_TERMINAL`.
8. **Failure modes** — daemon down → `API_CONNECTION_FAILED`; daemon up but SDK
   uninitialized → `A2A_SDK_NOT_INITIALIZED`; unknown task id → `TASK_NOT_FOUND`.
9. **Auth lifecycle** — expire the token mid-session; assert one silent re-auth and
   retry rather than a surfaced 401.

Scenarios 3–5 should run on both DynamoDB and PostgreSQL backends — the daemon sets
RLS context per call in PostgreSQL mode (`main.py:133-139`, `151-152`).

## 10. Risks

| Risk | Mitigation |
|---|---|
| A model treats `input_required` as failure and abandons a live task | §5.4 names it non-terminal in both descriptions; P4 scenario 5 asserts the full loop |
| `cancel_a2a_task` cascades further than the model expects | Cascade stated in the description; scoped to the caller's partition; P4 scenario 7 asserts idempotency |
| A long `message/stream` blocks for up to 330s (§6.1) | Ceiling is set above the slowest peer; on timeout the proxy returns `STREAM_TIMEOUT` without auto-cancel, and the caller recovers or cancels deliberately |
| `capabilities` reaches the model as a JSON string and it fails to match a capability | Parsed to a list in both discovery tools (§5.1), with raw-string fallback |
| A just-registered peer is missing from discovery because the list query is cached | Documented in the tool description so an empty result is not read as proof of absence (§5.1) |
| Two hand-written GraphQL documents drift from the daemon schema | P4 issues both against the live endpoint and asserts no `errors` |
| Model trusts empty registry `capabilities` and concludes a peer can do nothing | Description states the field is often unpopulated and that absence ≠ incapable; P4 scenario 1 asserts NULL surfaces as absent, not `[]`-meaning-none |
| Model mistakes this daemon's own card for a peer's capability document (§5.1) | `get_a2a_agent_card` is explicit that it returns only this daemon's card and accepts no `agent_id` in 0.1.0 |
| Capability data is too thin for the model to choose well | Known limitation, not a module bug — the fix is populating `capabilities` at registration (§5.1a ops recommendation); until then `agent_name` + backend metadata carry selection |
| Model delegates to an unsuitable peer using weak registry signals | Selection is deliberately the caller's (§5.1a); discovery exposes the available registry evidence, descriptions warn that missing capabilities do not mean incapability, and `agent_id` may be omitted to use the daemon default |

## 11. Out of Scope

- Agent / task / message / setting mutations — operator surface, external tool (§1)
- Push-notification webhook configs — the calling agent has no reachable endpoint
- Health and diagnostic tooling (`ping`)
- MCP transport, tool dispatch, and process lifecycle — `mcp_daemon_engine`'s concern
- SSE client lifecycle and live token delivery — gateway concern
- A2A protocol semantics, task state machine, persistence — daemon concern
- gRPC transport and GraphQL subscriptions — daemon-experimental

## 12. Resolved Questions

### Q1 — Which peers will be delegated to?

**Hermes and OpenClaw**, each with its own deployed gateway, plus the in-house
core-engine bridge:

| Peer | Handler | Deployment |
|---|---|---|
| Hermes Agent | `a2a_hermes_handler.HermesAgentHandler` | `docker-a2a-hermes-agent-gateway` |
| OpenClaw | `a2a_openclaw_handler` | `docker-a2a-openclaw-gateway` |
| `ai_agent_core_engine` | `a2a_core_engine_handler` | in-house bridge |

Which handler runs is decided by registry `metadata` (`module_name` / `class_name`), so
the proxy needs no peer-specific code. That metadata is ops configuration, not a
semantic capability — which is part of why capability-based auto-selection is left to
the calling agent rather than built into the proxy (§5.1a).

**Suggested P4 plan:** run discovery and delegation (scenarios 1–4) against **both**
Hermes and OpenClaw, since they are separately deployed and could diverge. Run the
multi-turn scenario (5) against **Hermes specifically** — it is the confirmed
`INPUT_REQUIRED` / human-approval producer (`a2a_ai_agent_utility.py:1026-1029`), and
its gateway is the one that exercises the HITL path end to end.

### Q2 — How long do real peer tasks run?

Up to **300s** by peer-handler default — so the originally-proposed 120 was wrong, and
`a2a_stream_timeout` is now **330**. Full reasoning and the source table are in §6.1.
The correction matters because 120 would have produced spurious timeouts against both
deployed peers while their work continued orphaned.

### Q3 — Does the registry populate `status` consistently?

**Yes — the `active` default is safe.** `insert_update` applies
`kwargs.get("status", "active")` (`models/dynamodb/a2a_agent.py:221`), so every agent
row has a status; the domain is `active` / `inactive` / `error`
(`a2a_agent.py:58`). Filtering runs through the `status-index` GSI, not a scan.

Researching this turned up two things that **do** change the implementation, both now
in §5.1:

- **`capabilities` comes back as a JSON string**, not a list — the write side
  `json_dumps`es it and the read side never reverses it. Both discovery tools must
  parse it. This supersedes the earlier plan to "document the asymmetry"; documenting
  it would have left the model reasoning over a quoted blob.
- **`resolve_a2a_agent_list` is cached** (`method_cache(ttl=…)`), so a freshly
  registered peer may not appear immediately.

One thing I checked and found **not** to be a problem: the `the_filters = None;
the_filters &= …` accumulator in the list resolvers looks like a `TypeError`, but
PynamoDB's `Condition.__rand__` explicitly special-cases `None & condition` for exactly
this idiom. `agent_name` filtering works.

## 13. Open Questions

Three earlier questions are **closed by sibling convention** rather than needing an
answer:

- *Where do argument defaults live?* In the tool's `inputSchema`
  (`mcp_kg_inquirer/client.py:33-69`), applied by the host before the method runs — not
  in method code. So `status="active"`, `limit=20`, `extended=false` are schema
  defaults.
- *Should the streaming tool be `is_async`?* No sibling module uses it; every tool in
  `mcp_hospirfq_processor`, `mcp_kg_inquirer` and `mcp_resolvepay_connector` is a plain
  sync `def`. This module stays sync, bounded by `a2a_stream_timeout`, and matches its
  siblings. Revisit only if §10's timeout risk actually bites.
- *How should "no results" be reported?* A plain sentence, not an error dict
  (`catalog_mixin.py:53-54`).

**Remaining — all empirical, none blocking P0–P3:**

1. **Measure real peer durations in P4.** 330s is derived from handler defaults, not
   from observed behaviour. If the p99 is well under it, lower the ceiling.
2. **Are remote A2A peers expected to become the norm?** If registry entries will mostly
   point at independent A2A servers rather than local handlers, the deferred remote
   card fetch (§5.1a) becomes worth doing — and the §1 "only talks to its own daemon"
   boundary gets an explicit, documented exception. Today the reverse is true.
3. **Reachability from the MCP gateway to the peer gateways** is a deployment routing
   question — relevant to the daemon's handlers, not to this module, unless (2) changes.



