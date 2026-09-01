'use strict';

// Offline contract test for preload.cjs. No Claude process and no network request.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const output = path.join(os.tmpdir(), `claude-turn-headers-${process.pid}.jsonl`);
process.env.CLAUDE_TURN_HEADERS_FILE = output;

const calls = [];
globalThis.fetch = async (input, init) => {
  calls.push({ input, init });
  return new Response('{}', {
    status: 200,
    headers: {
      'request-id': 'req_offline_test',
      'anthropic-ratelimit-requests-remaining': '42',
      'set-cookie': 'must-not-leak=1',
    },
  });
};

require('./preload.cjs');

(async () => {
  try {
    const init = {
      method: 'POST',
      headers: {
        Authorization: 'Bearer secret-setup-token',
        'anthropic-beta': 'oauth-2025-04-20,test-beta',
        'x-app': 'cli',
      },
      body: '{"model":"claude-sonnet-test","test":true}',
    };

    const response = await fetch('https://api.anthropic.com/v1/messages?beta=true', init);
    assert.equal(response.status, 200);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].init, init, 'preload must pass the original init object through');

    const events = fs.readFileSync(output, 'utf8').trim().split('\n').map(JSON.parse);
    assert.deepEqual(events.map((event) => event.kind), [
      'preload_ready', 'request', 'response',
    ]);
    assert.equal(events[1].model, 'claude-sonnet-test');
    assert.equal(events[1].headers.authorization, 'Bearer <redacted>');
    assert.equal(events[1].headers['anthropic-beta'], 'oauth-2025-04-20,test-beta');
    assert.equal(events[2].headers['request-id'], 'req_offline_test');
    assert.equal(events[2].headers['anthropic-ratelimit-requests-remaining'], '42');
    assert.equal(events[2].headers['set-cookie'], '<redacted>');

    console.log('PASS: fetch capture and redaction (offline)');
  } finally {
    fs.rmSync(output, { force: true });
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
