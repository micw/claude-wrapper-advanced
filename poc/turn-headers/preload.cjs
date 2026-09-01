'use strict';

// Loaded into the compiled Claude Code Bun executable via:
//   BUN_OPTIONS="--preload=/absolute/path/to/preload.cjs"
//
// Captures the headers visible at the fetch boundary. It neither reads nor
// replaces request/response bodies and must never make a failed capture fail a turn.

const fs = require('node:fs');

const originalFetch = globalThis.fetch;
const captureFile = process.env.CLAUDE_TURN_HEADERS_FILE;
const captureFd = parseFd(process.env.CLAUDE_TURN_HEADERS_FD);
let sequence = 0;

function parseFd(value) {
  if (value === undefined || value === '') return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : null;
}

function enabled() {
  return captureFd !== null || Boolean(captureFile);
}

function emit(value) {
  if (!enabled()) return;

  try {
    const line = `${JSON.stringify({
      capture: 'claude-turn-headers-v1',
      at: new Date().toISOString(),
      pid: process.pid,
      ...value,
    })}\n`;

    if (captureFd !== null) {
      fs.writeSync(captureFd, line);
    } else {
      fs.appendFileSync(captureFile, line, { mode: 0o600 });
    }
  } catch {
    // Observability must not alter the CLI request or its outcome.
  }
}

function requestUrl(input) {
  try {
    if (typeof input === 'string' || input instanceof URL) return new URL(input);
    if (input && typeof input.url === 'string') return new URL(input.url);
  } catch {
    return null;
  }
  return null;
}

function isTurn(url) {
  return url !== null && url.pathname === '/v1/messages';
}

function effectiveHeaders(input, init) {
  // Fetch semantics: init.headers, when present, replaces Request.headers. It is
  // not merged with it. Avoid constructing a new Request: that can lock or consume
  // a streaming request body.
  try {
    if (init && Object.prototype.hasOwnProperty.call(init, 'headers')) {
      return new Headers(init.headers);
    }
    if (typeof Request !== 'undefined' && input instanceof Request) {
      return new Headers(input.headers);
    }
  } catch {
    // Fall through to an empty set; the request itself is still sent unchanged.
  }
  return new Headers();
}

function requestModel(init) {
  // A CLI turn can issue side requests with a different model. Read only the
  // top-level model discriminator; never emit or retain the body itself.
  const body = init?.body;
  if (typeof body !== 'string' || body.length > (64 << 20)) return null;
  try {
    const parsed = JSON.parse(body);
    return typeof parsed?.model === 'string' ? parsed.model : null;
  } catch {
    return null;
  }
}

function capturedHeaders(headers) {
  const result = {};
  for (const [name, value] of headers.entries()) {
    const lower = name.toLowerCase();
    if (lower === 'authorization') {
      result[lower] = /^Bearer\s+/i.test(value) ? 'Bearer <redacted>' : '<redacted>';
    } else if (lower === 'x-api-key' || lower === 'cookie' || lower === 'set-cookie') {
      result[lower] = '<redacted>';
    } else {
      result[lower] = value;
    }
  }
  return result;
}

if (typeof originalFetch === 'function' && enabled()) {
  globalThis.fetch = async function capturedFetch(input, init) {
    const url = requestUrl(input);
    if (!isTurn(url)) return originalFetch.call(this, input, init);

    const id = ++sequence;
    const started = Date.now();
    emit({
      kind: 'request',
      id,
      method: String(init?.method || input?.method || 'GET').toUpperCase(),
      url: url.toString(),
      model: requestModel(init),
      headers: capturedHeaders(effectiveHeaders(input, init)),
    });

    try {
      const response = await originalFetch.call(this, input, init);
      emit({
        kind: 'response',
        id,
        url: url.toString(),
        status: response.status,
        elapsed_ms: Date.now() - started,
        headers: capturedHeaders(response.headers),
      });
      return response;
    } catch (error) {
      emit({
        kind: 'fetch_error',
        id,
        url: url.toString(),
        elapsed_ms: Date.now() - started,
        error: error instanceof Error ? error.name : typeof error,
        message: error instanceof Error ? error.message : String(error),
      });
      throw error;
    }
  };

  emit({ kind: 'preload_ready' });
}
