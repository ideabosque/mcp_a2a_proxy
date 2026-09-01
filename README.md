# MCP A2A Proxy

An **MCP→A2A adapter** for the SilvaEngine platform: it lets a tool-calling agent act as an A2A client, communicating with other agents over the A2A protocol on its behalf. The calling agent gets A2A participation without implementing any of A2A — it just calls tools.

## Architecture

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

The proxy does **not** open A2A connections to arbitrary remote agents. It always talks to the local `a2a_daemon_engine` (through `silvaengine_gateway`). The daemon owns the A2A server interface and brokers to backends through per-agent handler metadata.

## Tool Surface — 8 tools

One loop: **discover → delegate → track → abandon.**

| Tool | Domain | Method | Purpose |
|------|--------|--------|---------|
| `discover_a2a_agents` | Discovery | GraphQL `a2aAgentList` | Find peers — who exists |
| `get_a2a_agent` | Discovery | GraphQL `a2aAgent` | One peer's registry record |
| `get_a2a_agent_card` | Discovery | REST GET / JSON-RPC | This daemon's own card |
| `send_a2a_message` | Delegation | JSON-RPC `message/send` | Delegate work |
| `send_a2a_message_stream` | Delegation | JSON-RPC `message/stream` | Delegate, collecting all events |
| `get_a2a_task` | Task tracking | JSON-RPC `tasks/get` | Status, result, history |
| `list_a2a_tasks` | Task tracking | JSON-RPC `tasks/list` | Recover in-flight work |
| `cancel_a2a_task` | Task tracking | JSON-RPC `tasks/cancel` | Abandon work (cascades to children) |

## Installation

```bash
pip install -e ".[dev]"
```

## Testing

```bash
python -m pytest mcp_a2a_proxy/tests/ -v
```

## Configuration

Settings are stored per partition in the MCP registry and passed to `__init__`:

```python
{
    "gateway_base_url": "http://localhost:8765",
    "a2a_jsonrpc_endpoint": "http://localhost:8765/{endpoint_id}/a2a",
    "a2a_agent_card_endpoint": "http://localhost:8765/{endpoint_id}/.well-known/agent-card.json",
    "graphql_modules": {
        "a2a_daemon_engine": {
            "endpoint": "http://localhost:8765/{endpoint_id}/a2a_core_graphql",
            "gateway_base_url": "http://localhost:8765",
            "token_username": "svc",
            "token_password": "replace-me",
        }
    },
    "a2a_stream_timeout": 330,
    "default_page_limit": 20,
}
```

### Why `a2a_stream_timeout` is 330, not 120

The proxy waits on the daemon, which waits on the peer's handler. The peer handler defaults are what actually bound a delegation:

| Peer handler | Default |
|---|---|
| Hermes/OpenClaw handler | 300s |
| Core engine bridge | 120s |

Setting the proxy's client timeout to 120 would cut off a Hermes or OpenClaw peer entitled to 300s — producing the worst failure mode: the agent receives `STREAM_TIMEOUT` while the peer keeps running, so the work is orphaned *and* the error is wrong. The client timeout must exceed the slowest peer: **330s** (300 + 30s margin).

## Multi-turn (INPUT_REQUIRED)

A peer can pause mid-task to ask for input or human approval. No extra tool is needed; the loop composes:

```text
send_a2a_message(message, agent_id)          -> task_id, state = input_required
get_a2a_task(task_id, history_length=N)      -> read the peer's question
send_a2a_message(message=answer, task_id=…)  -> resumes the SAME task
get_a2a_task(task_id)                        -> completed + result
```

`input_required` is a **non-terminal** state — the peer is waiting on you.

## License

MIT — © IdeaBosque