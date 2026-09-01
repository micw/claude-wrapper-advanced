# PoC: HTTP headers of Claude CLI turns

This PoC records request and response headers for the CLI's normal
`POST /v1/messages` turns. It does **not** use `/api/oauth/usage`, which is not
available with a `setup-token`, and it does not proxy or decrypt TLS.

Claude Code is distributed as a compiled Bun executable. Bun standalone
executables honor `BUN_OPTIONS`; `--preload` lets `preload.cjs` wrap the global
`fetch` before the embedded Anthropic SDK obtains it.

## What is captured

For every URL whose path is exactly `/v1/messages`, two JSONL events are written:

- `request`: method, URL, requested model, and the headers passed to `fetch` by the
  Anthropic SDK.
- `response`: status and all response headers exposed by Fetch, including any
  `anthropic-ratelimit-*`, `retry-after`, and `request-id` headers sent upstream.

A `fetch_error` replaces the response event when Fetch rejects. Every event has a
per-process sequence `id`, so request and response can be paired. A single
`preload_ready` event proves that the preload ran.

The request body is never recorded or changed. Its top-level `model` field is parsed
because one CLI turn can make internal side requests using a different model. The
response body is never inspected. `Authorization`, `x-api-key`, `cookie`, and
`set-cookie` values are always redacted. In particular, `Authorization: Bearer ...`
is retained only as `Bearer <redacted>`.

This captures the application headers at the Fetch boundary. Transport-generated
headers such as `Host`, HTTP/2 pseudo-headers, and a computed `Content-Length` may
not yet exist there. They are not relevant to reproducing quota semantics, and
capturing them would require instrumenting Bun below Fetch or changing the network
path.

## Offline check

This uses a fake Fetch implementation. It starts neither Claude nor a network
request:

```bash
node poc/turn-headers/offline-test.cjs
```

## Manual capture later

Do not run this while another measurement must remain undisturbed. When ready,
pass the complete command explicitly; the runner has no default invocation:

```bash
poc/turn-headers/run.sh claude -p \
  --output-format stream-json \
  --verbose \
  'Reply with one word.'

jq . claude-turn-headers.jsonl
```

An explicit destination can be selected without changing the child command:

```bash
CLAUDE_TURN_HEADERS_FILE=/tmp/turn.jsonl \
  poc/turn-headers/run.sh claude ...
```

`run.sh` preserves existing `BUN_OPTIONS`, creates/truncates the capture with a
`077` umask, and then `exec`s exactly the supplied command.

## Live observation

Verified with Claude Code 2.1.198, 2.1.220 and the wrapper's pinned 2.1.252, using
real setup-token turns on 2026-09-01:

- the preload ran and the turn completed normally;
- one CLI turn made two concurrent `POST /v1/messages?beta=true` requests;
- their bodies named `claude-haiku-4-5` and `claude-haiku-4-5-20251001`, confirming
  that attribution must use each request's body rather than only the process bucket;
- both responses exposed the complete `5h` and `7d` utilization/reset/status sets,
  plus unified status, representative claim, overage state and fallback percentage;
- response order differed from request order, and the two 5h readings straddled a
  percentage boundary (`0.08` and `0.09`). A production merge must therefore pair by
  capture ID and, within the same reset window, never move utilization backwards merely
  because concurrent responses arrive in a different order.

## Wrapper integration, deliberately not part of this PoC

A production integration should avoid a shared file and use the preload's
`CLAUDE_TURN_HEADERS_FD` output instead:

1. Create one pipe per CLI process with `os.pipe()`.
2. Pass the write side through `asyncio.create_subprocess_exec(..., pass_fds=...)`.
3. Set `CLAUDE_TURN_HEADERS_FD` and append the preload to `BUN_OPTIONS` in the
   child environment.
4. Parse JSONL from the read side and associate each `/v1/messages` exchange with
   the process's active turn.
5. Keep request and response events separately: a CLI turn can make more than one
   Messages request because of retries or internal side calls.

Before integration, verify the PoC against every pinned Claude version. The
wrapper should fail open when capture breaks: missing header telemetry must never
fail inference.
