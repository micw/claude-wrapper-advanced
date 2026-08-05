# claude-wrapper-advanced

**An advanced wrapper that exposes the Claude Code CLI as an OpenAI-compatible REST API** —
driven by your **Pro/Max subscription** instead of separate API credits.

Endpoints: `/v1/chat/completions` and `/v1/responses` (both streaming + non-streaming), `/v1/models`,
plus `/healthz` and `/metrics`.
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
- **Vision** — inline OpenAI `image_url` parts (base64 data URI) become native image blocks, so
  pasting a screenshot in Open WebUI & Co. just works, in the current turn and in history.
- **Visible thinking progress** — opus at high effort can reason for minutes before the first
  token. We stream `reasoning_content` lines (`Thinking… · 2.9k tokens`) so the client shows
  progress instead of a dead connection. The CLI redacts the reasoning *text*, so this is a
  progress indicator built from `estimated_tokens`, never invented reasoning.
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
- **Images** are the exception to "flatten to text": they are passed to the CLI as native `image`
  blocks (base64), placed right before the text of their message. Images that can't be passed through
  (too large, unsupported format, remote URL) are dropped with a note in the history, so the model
  says *what* is missing instead of ignoring it.
- **Remote image URLs are deliberately not supported.** Letting the backend fetch them
  (`source.type: "url"`) fails in practice — robots.txt, or hosts it can't reach such as Open WebUI's
  own `/cache/image/…` — and that failure 400s the *whole* request. Fetching them in the wrapper
  instead would break determinism: the history is re-sent every turn, so the same URL would be
  re-fetched every turn, and any byte change invalidates the cache prefix. If someone wants an image
  from a link, a web-fetch tool on the client side is the right place — then the content is a regular
  part of the session instead of an invisible side effect.
- The request's tools are declared as **real MCP tools** → Claude emits a **native** `tool_use`. Our MCP server **stalls** on the call, we read the call from the stream and return it as OpenAI `tool_calls` (the **client** executes the tool).
- Process model: a **reuse pool** keeps warm CLI processes alive and recycles them via `/clear` (bucketed by model + toolset). It falls back to one-shot when disabled (`POOL_ENABLED=0`).

## The Responses API (`/v1/responses`)

The OpenAI Responses API is supported **in addition to** Chat Completions — same pipeline, same
prompt building, so history flattening, images, tool capture and prompt caching behave identically.
Point a client at it by setting that connection's API type to `responses` (Open WebUI: *Connections
→ API type*); it then POSTs to `<base-url>/responses`.

What it buys over Chat Completions: reasoning is its own typed output item, so the thinking progress
lives in `summary` where it belongs instead of in the same field other models use for real reasoning
text — and clients **replace** that summary part rather than appending it, so the line updates in
place (hence the much shorter `THINKING_INTERVAL_RESPONSES`).

Supported: `input` as a string or as items (`message` with `input_text`/`output_text`/`input_image`,
`function_call`, `function_call_output`), `instructions`, `tools` in the flat Responses form,
`stream`, and the `model` suffix / `reasoning.effort` for effort control. Streaming emits
`response.created → in_progress → output_item.added/done → completed`, with
`response.output_text.delta`, `response.function_call_arguments.delta/done` and
`response.reasoning_summary_part.added/done`.

The terminal event is always `response.completed`, even when the status inside it is
`incomplete`. The spec would call for `response.incomplete`, but clients do not act on it — Open
WebUI's handler returns no metadata for it, so `usage` and the done signal are lost and the message
never finishes. The status and `incomplete_details` are in the envelope either way.

`usage` carries `output_tokens_details.reasoning_tokens` — the summed `estimated_tokens` from the
CLI's thinking events, capped at `output_tokens` because it is an estimate, not a billed figure.
The chat endpoint reports the same under `completion_tokens_details.reasoning_tokens`. A truncated
answer comes back as `status: "incomplete"` with `incomplete_details`, mirroring
`finish_reason: "length"` on the chat side.

**Deliberately not supported: server-side state.** `previous_response_id` is rejected with a 400 —
silently ignoring it would answer with half the conversation missing, which surfaces as a wrong
answer rather than an error. `store` is accepted and ignored, since we never persist anything.
Open WebUI is stateless by default (`ENABLE_RESPONSES_API_STATEFUL=False`), so this needs no
configuration. `store` is accepted and ignored, `background: true` is rejected (nothing would be
stored to poll for), and `GET`/`DELETE /v1/responses/{id}` plus `/cancel` answer **501** rather than
a 404 that would read like "unknown id". Built-in server-side tools (`web_search`, `file_search`, …)
are dropped: they would run inside OpenAI's infrastructure, which we are not.

Not implemented: structured outputs (`text.format`) — the CLI cannot enforce a JSON schema, and
faking it in the prompt would promise a guarantee we cannot keep. `max_output_tokens`, `temperature`
and `top_p` are ignored, exactly as on the chat endpoint, because the CLI exposes no such knobs.

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
only).

**The bundled CLI is pinned (`CLAUDE_VERSION=2.1.220`) on purpose** — only versions the assumption
tests have passed on get shipped. To move the pin up, vet the new version first, then bump it in the
Dockerfile:

```bash
npm install @anthropic-ai/claude-code@<x.y.z> --prefix /tmp/cli
CLAUDE_BIN=/tmp/cli/node_modules/@anthropic-ai/claude-code-linux-x64/claude \
  python tests/assumptions.py
```

**Don't run the proxy from inside a Claude Code session** without the env scrubbing the wrapper does
for you (`child_env()` in [`app/cli_driver.py`](app/cli_driver.py)). A parent session exports
`CLAUDE_CODE_ENTRYPOINT`, and since CLI 2.1.198 the child then puts a scratchpad path *with a session
UUID* into its system prompt. Every `/clear` mints a new UUID, so the cached prefix never matches and
the entire history is re-written each turn — measured: 100% `cache_read` drops to 0%, nothing errors,
it just gets ~18× more expensive per follow-up turn. `env.no_parent_session` guards this.

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
- **No reasoning/thinking *text*.** The CLI emits `thinking_delta` events while the model reasons,
  but they are redacted — `{"thinking": "", "estimated_tokens": 150}`. We forward the progress line
  described above, never the reasoning itself, because there is none to forward.
  `cli.thinking_is_redacted` fails the day this changes.
- Latency is inference-dominated (~3s/turn; one tool round-trip = 2 turns).
- **Timeouts are idle-based, not wall-clock.** A turn is aborted after `IDLE_TIMEOUT` seconds of
  *silence*, not after a fixed total duration — a total deadline kills long but healthy turns
  (production: first token at 164.9s, killed by the old 180s cap at 180s). `REQUEST_TIMEOUT` is
  just a backstop. Raise `IDLE_TIMEOUT` only if `cli.streams_continuously` reports gaps near it.
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
python -m unittest discover -s tests -t .   # unit tests: free, ~5ms, no CLI/backend needed
python tests/assumptions.py --offline       # fast, no backend
python tests/assumptions.py                 # full (needs login, costs a few tokens)
```

Everything decidable without the model lives in [`tests/test_translate.py`](tests/test_translate.py)
as plain `unittest` (no pytest dependency) — history flattening, image handling and limits, and the
byte-stability of the history prefix that the whole caching design rests on.

See [tests/README.md](tests/README.md) for the workflow and how to add an assumption.
