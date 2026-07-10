# claude-wrapper-advanced

**An advanced wrapper that exposes the Claude Code CLI as an OpenAI-compatible REST API** —
driven by your **Pro/Max subscription** instead of separate API credits.

Endpoints: `/v1/chat/completions` (streaming + non-streaming), `/v1/models`, plus `/healthz` and `/metrics`.
Design, rationale and the empirical findings live in [KONZEPT.md](KONZEPT.md).

## Why "advanced"?

A naive wrapper just pipes text through `claude -p`. This one does the hard parts that make it a
genuine drop-in OpenAI backend:

- **Native tool calling** — a request's `tools` become real MCP tools, so Claude emits a *native*
  `tool_use`; we capture it (MCP stall + interrupt) and return standard OpenAI `tool_calls`. No brittle
  scraping of the model's prose.
- **Faithful history replay** — the entire OpenAI `messages` array (including prior tool calls/results)
  is reconstructed into a single prompt the CLI accepts, so multi-turn conversations and tool loops work.
- **Warm process pool** — CLI processes stay alive and are recycled via `/clear`, bucketed by
  model + toolset, with liveness checks, retry-on-dead and idle eviction. Saves the ~0.8 s spawn/init per call.
- **Prompt-cache aware** — a stable tool/system prefix yields high cache-hit rates (tracked live at `/metrics`).
- **Per-request effort control** — OpenAI `reasoning_effort`, OpenRouter `reasoning.effort`, or a
  model-name suffix like `opus:max` (the model picker doubles as an effort selector).
- **Real usage & cost** — OpenAI `usage` plus an OpenRouter-style `cost`, with cache read/write token stats.
- **Observability** — `/metrics` exposes latency bands (ttft / spawn / overhead), cache hit-rate and the
  account-wide rate-limit status.
- **Subscription-native & ToS-clean** — uses the official CLI login, never extracts tokens or touches the
  raw API. Ships as a non-root container with in-container login.

## How it works (in short)

- The entire OpenAI history is flattened into **one** prompt (otherwise the CLI would reply to every user message).
- Earlier tool calls/results are rendered as **text** (the CLI rejects injected tool blocks — but it trusts the text).
- The request's tools are declared as **real MCP tools** → Claude emits a **native** `tool_use`. Our MCP server **stalls** on the call, we read the call from the stream and return it as OpenAI `tool_calls` (the **client** executes the tool).
- Process model: a **reuse pool** keeps warm CLI processes alive and recycles them via `/clear` (bucketed by model + toolset). It falls back to one-shot when disabled (`POOL_ENABLED=0`).

## Requirements

1. **Claude Code CLI installed and logged in:**
   ```bash
   claude          # start once and run /login
   claude auth status   # should show "logged in"
   ```
2. Python 3.11+.

## Install & run

```bash
cd ~/git/claude-test
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # optionally adjust (port, API_KEY, DEFAULT_MODEL)
./run.sh                  # or: uvicorn app.main:app --port 8000
```

The server then runs on `http://127.0.0.1:8000`.

## Docker

The image bundles the official Claude Code CLI (via npm) and runs as a **non-root** user
(Claude Code refuses `--dangerously-skip-permissions` as root, which the MCP tool path needs).

```bash
cp .env.example .env        # optional: adjust API_KEY, DEFAULT_MODEL, PROXY_PORT
docker compose up -d --build
```

The container **starts even without authentication** — it stays up and logs a login hint so you
can sign in from inside. There are two ToS-clean ways to authenticate your subscription:

**A) Interactive login (recommended, persistent).** Log in once inside the running container;
credentials land in a mounted volume and the CLI refreshes them itself:

```bash
docker compose exec proxy claude /login     # opens a URL — authorize, paste the code back
docker compose restart proxy                # optional; picks up the login immediately
curl -s localhost:8000/healthz | jq         # -> "authenticated": true
```

**B) Long-lived token (headless/CI).** `claude setup-token` is the official subscription-scoped
command (not credential extraction — ToS-clean). Generate it, then set it in `.env`:

```bash
docker compose exec proxy claude setup-token   # prints a ~1-year token
# put it in .env as CLAUDE_CODE_OAUTH_TOKEN=..., then:
docker compose up -d
```

Until authenticated, `/v1/*` requests return **503** with a clear message, and `/healthz`
reports `"authenticated": false`. The published port is `127.0.0.1:${PROXY_PORT:-8000}` (localhost
only); pin the CLI with `CLAUDE_VERSION=<x.y.z>` as a build arg for reproducible images.

## Quick test (curl)

```bash
# Models
curl -s localhost:8000/v1/models | jq

# Chat (non-streaming)
curl -s localhost:8000/v1/chat/completions -H 'content-type: application/json' -d '{
  "model":"sonnet",
  "messages":[{"role":"user","content":"Say hello in exactly one word."}]
}' | jq '.choices[0].message'

# Tool call (model should request the tool -> finish_reason=tool_calls)
curl -s localhost:8000/v1/chat/completions -H 'content-type: application/json' -d '{
  "model":"sonnet",
  "messages":[{"role":"user","content":"What is the weather in Berlin? Use the tool."}],
  "tools":[{"type":"function","function":{"name":"get_weather","description":"Live weather for a city",
    "parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]
}' | jq '.choices[0]'

# Streaming
curl -sN localhost:8000/v1/chat/completions -H 'content-type: application/json' -d '{
  "model":"sonnet","stream":true,
  "messages":[{"role":"user","content":"Count from 1 to 5."}]
}'
```

## VS Code / Copilot

Copilot Chat can use its own OpenAI-compatible endpoints (BYOK). Prefer VS Code's built-in
**Custom Endpoint** provider (`vendor: "customendpoint"`, `apiType: "chat-completions"`) — no third-party
extension needed. Note that VS Code does **not** auto-discover models via `/v1/models`; you list them
manually, and the token window shown in the UI comes from each model's `maxInputTokens` in the config
(not from this API):

```json
{
  "name": "Claude-CLI",
  "vendor": "customendpoint",
  "apiType": "chat-completions",
  "apiKey": "any-value-if-API_KEY-empty",
  "models": [
    { "id": "opus",   "url": "http://127.0.0.1:8000/v1/chat/completions",
      "maxInputTokens": 1000000, "maxOutputTokens": 32000,
      "capabilities": { "toolCalling": true } },
    { "id": "sonnet", "url": "http://127.0.0.1:8000/v1/chat/completions",
      "maxInputTokens": 200000,  "maxOutputTokens": 16000 }
  ]
}
```

Set `apiKey` to any value if `API_KEY` in `.env` is empty; otherwise use exactly that value.
Alternatively, extensions like **Continue** or **Cline** accept any OpenAI-compatible URL — point them at
`http://localhost:8000/v1`.

Always run the curl quick test before testing in the editor.

## Known limitations (details in KONZEPT.md)

- **No parallel `tool_calls`** (max. 1 tool call per response; multi-tool is sequential).
- **No reasoning/thinking text** (the CLI does not emit it).
- Latency is inference-dominated (~3s/turn; one tool round-trip = 2 turns).
- Per-request `cost` in `usage` is distorted for tool-call turns (cumulative cost is correct); see the pool notes in the code.

## Assumption tests

This proxy is built on ~30 behaviours of the Claude Code CLI and the Anthropic backend that were
established empirically (the CLI replies to every message, native `tool_use` capture, text-injected
tool results are trusted, block-level prompt caching, the `ttl` requirement on `cache_control`, the
result/usage JSON shape, …). A CLI update can silently break any of them.

[`tests/assumptions.py`](tests/assumptions.py) encodes these as an executable checklist that exercises
the real CLI **and** our wrapper, and reports PASS/FAIL/SKIP per assumption. Run it whenever the CLI
is upgraded — Tier 1 is offline and free (catches renamed/removed flags instantly), Tier 2 verifies
behaviour against the backend:

```bash
python tests/assumptions.py --offline   # fast, no backend
python tests/assumptions.py             # full (needs login, costs a few tokens)
```

See [tests/README.md](tests/README.md) for the workflow and how to add an assumption.
