'use strict';

// Runs inside the compiled Bun CLI. It observes only /v1/messages metadata and writes
// JSONL to a dedicated inherited fd. Capture is deliberately fail-open: it must never
// alter a request, response body, or turn outcome.
const fs = require('node:fs');

const originalFetch = globalThis.fetch;
const fd = Number(process.env.CLAUDE_TURN_HEADERS_FD);
let sequence = 0;

function emit(value) {
  if (!Number.isInteger(fd) || fd < 0) return;
  try {
    fs.writeSync(fd, `${JSON.stringify({capture: 'claude-turn-headers-v1', ...value})}\n`);
  } catch {
    // Telemetry must never break inference.
  }
}

function requestUrl(input) {
  try {
    if (typeof input === 'string' || input instanceof URL) return new URL(input);
    if (input && typeof input.url === 'string') return new URL(input.url);
  } catch {}
  return null;
}

function requestModel(init) {
  // SDK request bodies are JSON strings. Parse only the model discriminator; neither
  // retain nor emit the prompt/body. The generous cap only prevents accidental parsing
  // of an unbounded nonstandard body and is above the wrapper's request limit.
  const body = init?.body;
  if (typeof body !== 'string' || body.length > (64 << 20)) return null;
  try {
    const parsed = JSON.parse(body);
    return typeof parsed?.model === 'string' ? parsed.model : null;
  } catch {
    return null;
  }
}

function limitHeaders(headers) {
  const result = {};
  try {
    for (const [name, value] of headers.entries()) {
      const lower = name.toLowerCase();
      if (lower.startsWith('anthropic-ratelimit-unified-')) result[lower] = value;
    }
  } catch {}
  return result;
}

if (typeof originalFetch === 'function' && Number.isInteger(fd) && fd >= 0) {
  globalThis.fetch = async function capturedFetch(input, init) {
    const url = requestUrl(input);
    if (!url || url.pathname !== '/v1/messages') return originalFetch.call(this, input, init);

    const id = ++sequence;
    emit({kind: 'request', id, model: requestModel(init)});
    try {
      const response = await originalFetch.call(this, input, init);
      emit({kind: 'response', id, status: response.status, headers: limitHeaders(response.headers)});
      return response;
    } catch (error) {
      emit({kind: 'fetch_error', id});
      throw error;
    }
  };
  emit({kind: 'preload_ready'});
}
