# claude-openai-proxy

OpenAI-compatible HTTP endpoint (`/v1/chat/completions`, `/v1/models`) built on top of the official
**Claude Code CLI** — uses your **Pro/Max subscription** instead of separate API credits.

Design, rationale and the empirical findings live in [KONZEPT.md](KONZEPT.md).

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
