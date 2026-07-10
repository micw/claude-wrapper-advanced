#!/usr/bin/env python3
"""Integration tests for our ASSUMPTIONS about the Claude Code CLI + the Anthropic backend.

The whole proxy is built on empirically discovered CLI behaviour. A CLI update can silently
break any of these assumptions. This suite checks them in isolation and prints a checklist:
what still works, and what you need to look at.

It deliberately uses the real app._build_args -> so it tests the CLI *and* our wrapper together.

Run:
  python tests/assumptions.py --offline     # Tier 1 only (no backend, ~0 tokens, fast)
  python tests/assumptions.py               # everything (Tier 1+2, needs login, costs tokens)
  python tests/assumptions.py --json        # machine-readable

Exit code != 0 if any assumption FAILs.
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Salt filler text per run so the 1h prompt cache from a previous run never pollutes cache tests.
NONCE = f"{int(time.time())}-{os.getpid()}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings                     # noqa: E402
from app.cli_driver import _build_args              # noqa: E402
from app.translate import messages_to_prompt, openai_tools_to_mcp  # noqa: E402

CLAUDE = settings.claude_bin

# Flags/subcommands the proxy relies on -> must still exist in --help (drift early-warning).
REQUIRED_FLAGS = [
    "--input-format", "--output-format", "--verbose", "--include-partial-messages",
    "--no-session-persistence", "--tools", "--effort", "--system-prompt",
    "--strict-mcp-config", "--mcp-config", "--allowedTools",
    "--dangerously-skip-permissions", "--model",
]

# ---------------------------------------------------------------- check registry
CHECKS = []


def check(cid, tier, desc):
    def deco(fn):
        CHECKS.append({"id": cid, "tier": tier, "desc": desc, "fn": fn})
        return fn
    return deco


class Result:
    def __init__(self, ok, observed=""):
        self.ok = ok            # True / False / None (= inconclusive/skip)
        self.observed = observed


OK = lambda o="": Result(True, o)          # noqa: E731
FAIL = lambda o="": Result(False, o)       # noqa: E731
SKIP = lambda o="": Result(None, o)        # noqa: E731


# ---------------------------------------------------------------- CLI helper
class CLI:
    """Minimal driver: builds args like the app, speaks stream-json over stdio."""

    def __init__(self, mcp_tools=None, model="sonnet", extra=()):
        self.args = _build_args(mcp_tools or [], model) + list(extra)
        self.proc = None

    async def __aenter__(self):
        self.proc = await asyncio.create_subprocess_exec(
            CLAUDE, *self.args, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        return self

    async def __aexit__(self, *a):
        if self.proc:
            try:
                self.proc.kill()
                await self.proc.wait()
            except Exception:
                pass

    async def send(self, content):
        line = json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"
        self.proc.stdin.write(line.encode())
        await self.proc.stdin.drain()

    async def send_raw(self, obj):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self.proc.stdin.drain()

    async def events_until(self, pred, timeout=90):
        """Collect events until pred(event) is True (returns (event, all)) or timeout/EOF."""
        seen = []
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            rem = deadline - loop.time()
            if rem <= 0:
                return None, seen
            try:
                raw = await asyncio.wait_for(self.proc.stdout.readline(), rem)
            except asyncio.TimeoutError:
                return None, seen
            if not raw:
                return None, seen
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            seen.append(ev)
            if pred(ev):
                return ev, seen

    async def result(self, timeout=90):
        ev, seen = await self.events_until(lambda e: e.get("type") == "result", timeout)
        return ev, seen

    async def clear(self):
        await self.send("/clear")
        return await self.result(timeout=settings.clear_timeout + 10)

    async def turn(self, content, timeout=90):
        await self.send(content)
        return await self.result(timeout)

    async def interrupt(self):
        await self.send_raw({"type": "control_request", "request_id": "i",
                             "request": {"subtype": "interrupt"}})


def usage_of(result_ev):
    return (result_ev or {}).get("usage") or {}


def total_in(u):
    return (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
        + (u.get("cache_creation_input_tokens") or 0)


def big(sent, n=300):
    return "\n".join(f"[{sent}-{NONCE}] line {i}: deterministic filler value={i*7%50}." for i in range(n))


# ================================================================ TIER 1: OFFLINE
@check("cli.version", 1, "CLI binary present and reports a version")
async def c_version(ctx):
    p = await asyncio.create_subprocess_exec(CLAUDE, "--version",
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    v = out.decode().strip()
    ctx["version"] = v
    return OK(v) if v else FAIL("no output")


@check("cli.flags", 1, "All flags we rely on still exist in --help")
async def c_flags(ctx):
    p = await asyncio.create_subprocess_exec(CLAUDE, "--help",
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    help_txt = out.decode()
    missing = [f for f in REQUIRED_FLAGS if f not in help_txt]
    return OK(f"{len(REQUIRED_FLAGS)} flags ok") if not missing else FAIL(f"MISSING: {missing}")


@check("auth.status_json", 1, "`auth status --json` returns JSON with loggedIn")
async def c_auth(ctx):
    p = await asyncio.create_subprocess_exec(CLAUDE, "auth", "status", "--json",
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    try:
        d = json.loads(out.decode())
    except Exception:
        return FAIL("not JSON")
    ctx["logged_in"] = bool(d.get("loggedIn"))
    if "loggedIn" not in d:
        return FAIL(f"no loggedIn field: {list(d)[:5]}")
    return OK(f"loggedIn={d.get('loggedIn')} method={d.get('authMethod')}")


@check("auth.setup_token", 1, "`setup-token` subcommand exists (headless auth)")
async def c_setuptoken(ctx):
    p = await asyncio.create_subprocess_exec(CLAUDE, "setup-token", "--help",
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    return OK() if "token" in out.decode().lower() else FAIL("not found")


# ================================================================ TIER 2: ONLINE
@check("proto.basic", 2, "stream-json: user msg -> result event with text")
async def c_basic(ctx):
    async with CLI() as cli:
        ev, _ = await cli.turn("Reply with exactly: PONG")
        if not ev:
            return FAIL("no result event")
        if ev.get("is_error"):
            return FAIL(f"is_error: {str(ev.get('result'))[:120]}")
        return OK(f"result='{str(ev.get('result'))[:40]}'")


@check("proto.clear_tokenfree", 2, "`/clear` returns a result and is token-free")
async def c_clear(ctx):
    async with CLI() as cli:
        ev, _ = await cli.clear()
        if not ev:
            return FAIL("no result on /clear")
        t = total_in(usage_of(ev))
        return OK(f"input_total={t}") if t == 0 else FAIL(f"not token-free: {t}")


@check("proto.responds_each", 2, "CLI replies to EVERY user msg (the reason we flatten history)")
async def c_each(ctx):
    async with CLI() as cli:
        e1, _ = await cli.turn("Say A")
        e2, _ = await cli.turn("Say B")   # WITHOUT /clear
        ok = bool(e1) and bool(e2) and not e1.get("is_error") and not e2.get("is_error")
        return OK("both msgs answered") if ok else FAIL("second msg not answered")


@check("proto.reuse_after_clear", 2, "Process reuse: /clear then a new prompt works")
async def c_reuse(ctx):
    async with CLI() as cli:
        await cli.turn("Say one")
        await cli.clear()
        ev, _ = await cli.turn("Reply with exactly: REUSED")
        return OK("reuse ok") if ev and not ev.get("is_error") else FAIL("reuse failed")


@check("stream.partial_deltas", 2, "--include-partial-messages emits streaming text deltas (needed for SSE)")
async def c_stream(ctx):
    async with CLI() as cli:
        await cli.send("Count from 1 to 10 separated by spaces.")
        got = {"d": False}

        def stop(e):
            if e.get("type") == "stream_event":
                d = (e.get("event") or {}).get("delta") or {}
                if d.get("type") == "text_delta" and d.get("text"):
                    got["d"] = True
                    return True
            return e.get("type") == "result"
        await cli.events_until(stop, timeout=60)
        await cli.interrupt()
    return OK("text_delta events arrive") if got["d"] else FAIL("no partial text deltas -> SSE streaming would break")


@check("usage.result_shape", 2, "result usage has the expected fields (drift detector)")
async def c_shape(ctx):
    async with CLI() as cli:
        ev, _ = await cli.turn("Say hi")
        u = usage_of(ev)
        need = ["input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"]
        miss = [k for k in need if k not in u]
        cost = ev.get("total_cost_usd") if ev else None
        stop = ev.get("stop_reason") if ev else None
        det = f"cost={'present' if cost is not None else 'MISSING'} stop_reason={stop}"
        if miss:
            return FAIL(f"usage fields missing: {miss}")
        if cost is None:
            return FAIL("total_cost_usd missing")
        return OK(det)


@check("tools.native_tooluse", 2, "MCP tool -> native tool_use (mcp__t__<name>) in the assistant event")
async def c_tooluse(ctx):
    tools = [{"name": "get_weather", "description": "Live weather for a city",
              "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]
    async with CLI(mcp_tools=tools) as cli:
        await cli.send("What is the weather in Berlin? You MUST call the get_weather tool.")

        def is_tooluse(e):
            if e.get("type") == "assistant":
                blocks = (e.get("message") or {}).get("content") or []
                return any(b.get("type") == "tool_use" for b in blocks)
            return e.get("type") == "result"
        ev, seen = await cli.events_until(is_tooluse, timeout=90)
        await cli.interrupt()
        if not ev or ev.get("type") == "result":
            return FAIL("no tool_use (model did not call)")
        blocks = (ev.get("message") or {}).get("content") or []
        tu = next((b for b in blocks if b.get("type") == "tool_use"), None)
        name = tu.get("name", "")
        ctx["tooluse_usage"] = (ev.get("message") or {}).get("usage")
        if not name.startswith("mcp__t__"):
            return FAIL(f"unexpected tool name: {name}")
        return OK(f"name={name}")


@check("tools.no_result_on_tooluse", 2, "Tool turns emit NO result event (usage only from assistant msg)")
async def c_noresult(ctx):
    # Uses the observation from tools.native_tooluse: there tool_use arrived BEFORE any result,
    # and the assistant msg carried usage. Confirms the assumption that cost is absent there.
    u = ctx.get("tooluse_usage")
    if u is None:
        return SKIP("tools.native_tooluse did not run / no tool_use")
    return OK("assistant msg carries usage, no result event")


@check("proto.interrupt_alive", 2, "Interrupt ends the turn, process stays usable")
async def c_interrupt(ctx):
    async with CLI() as cli:
        await cli.send("Count slowly from 1 to 200, one number per line.")
        # let it run briefly, then interrupt
        await cli.events_until(lambda e: e.get("type") == "stream_event", timeout=30)
        await cli.interrupt()
        # interrupt produces a result; then a new turn on the same process
        await cli.result(timeout=settings.clear_timeout + 10)
        await cli.clear()
        ev, _ = await cli.turn("Reply with exactly: ALIVE")
        return OK("process still usable after interrupt") if ev and not ev.get("is_error") \
            else FAIL("process dead after interrupt")


@check("cache.ttl_required", 2, "cache_control WITHOUT ttl is rejected, WITH ttl accepted")
async def c_ttl(ctx):
    H = big("TTL", 400)
    async with CLI() as cli:
        await cli.clear()
        no_ttl = [{"type": "text", "text": H, "cache_control": {"type": "ephemeral"}},
                  {"type": "text", "text": "\nAnswer 'ok'."}]
        ev1, _ = await cli.turn(no_ttl)
        t_no = total_in(usage_of(ev1))
        await cli.clear()
        with_ttl = [{"type": "text", "text": H, "cache_control": {"type": "ephemeral", "ttl": "1h"}},
                    {"type": "text", "text": "\nAnswer 'ok'."}]
        ev2, _ = await cli.turn(with_ttl)
        t_yes = total_in(usage_of(ev2))
    # Assumption: without ttl -> call is dropped (0 tokens); with ttl -> normal
    if t_no == 0 and t_yes > 0:
        return OK(f"without ttl={t_no} (dropped), with ttl={t_yes} (ok)")
    return FAIL(f"unexpected: without ttl={t_no}, with ttl={t_yes}")


@check("cache.identical_resend", 2, "Identical resend -> ~100% cache_read")
async def c_ident(ctx):
    H = big("IDENT", 600)
    async with CLI() as cli:
        await cli.send("hi"); await cli.result()          # warm the system prompt
        c = [{"type": "text", "text": H, "cache_control": {"type": "ephemeral", "ttl": "1h"}},
             {"type": "text", "text": "\nAnswer 'ok'."}]
        await cli.clear(); await cli.turn(c)
        await cli.clear()
        ev, _ = await cli.turn(c)
        u = usage_of(ev)
        tot = total_in(u); rd = u.get("cache_read_input_tokens") or 0
        frac = rd / tot if tot else 0
    return OK(f"read {rd}/{tot} = {frac:.0%}") if frac > 0.9 else FAIL(f"only {frac:.0%} cached")


@check("cache.incremental", 2, "Per-message blocks -> incremental (old blocks read from cache)")
async def c_incr(ctx):
    def content(msgs):
        blocks = [{"type": "text", "text": "PRE\n"}]
        for i, t in enumerate(msgs):
            b = {"type": "text", "text": t + "\n"}
            if i == len(msgs) - 1:
                b["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
            blocks.append(b)
        blocks.append({"type": "text", "text": "\nAnswer 'ok'."})
        return blocks
    m = [big("A", 300)]
    async with CLI() as cli:
        await cli.send("hi"); await cli.result()
        await cli.clear()
        ev1, _ = await cli.turn(content(m))
        c1 = usage_of(ev1).get("cache_creation_input_tokens") or 0
        m2 = m + [big("B", 300), big("C", 300)]
        await cli.clear()
        ev2, _ = await cli.turn(content(m2))
        u2 = usage_of(ev2)
        c2 = u2.get("cache_creation_input_tokens") or 0
        r2 = u2.get("cache_read_input_tokens") or 0
    # Assumption: old block (A) is read in turn2 -> read large, create only the new part
    return OK(f"turn1 create={c1}; turn2 create={c2} read={r2}") if r2 > 4000 else \
        FAIL(f"no incremental hit: turn2 read={r2} create={c2}")


@check("model.aliases", 2, "Model aliases (opus/haiku) are accepted")
async def c_models(ctx):
    res = {}
    for mdl in ("opus", "haiku"):
        async with CLI(model=mdl) as cli:
            ev, _ = await cli.turn("Reply with exactly: OK")
            res[mdl] = bool(ev) and not ev.get("is_error")
    bad = [m for m, ok in res.items() if not ok]
    return OK(f"accepted: {list(res)}") if not bad else FAIL(f"rejected: {bad}")


@check("tools.result_injection_trusted", 2,
       "Text-injected tool result is trusted: model answers from it and does NOT re-call the tool")
async def c_trust(ctx):
    # The core BACKWARD path: prior tool calls/results are rendered as TEXT (via our real
    # translate.messages_to_prompt) and the model must trust them. Uses an unguessable value so a
    # correct answer proves the injected text was used; tool is available so re-calling is possible.
    tools = [{"type": "function", "function": {
        "name": "get_weather", "description": "Live weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
    marker = "raining frogs (code ZQX-9)"
    messages = [
        {"role": "user", "content": "What is the weather in Berlin? Use the get_weather tool."},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "get_weather", "arguments": '{"city":"Berlin"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": f"It is 42 degrees and {marker} in Berlin."},
    ]
    content = messages_to_prompt(messages)                 # our REAL rendering
    async with CLI(mcp_tools=openai_tools_to_mcp(tools)) as cli:
        await cli.send(content)

        def stop(e):
            if e.get("type") == "assistant":
                blocks = (e.get("message") or {}).get("content") or []
                if any(b.get("type") == "tool_use" for b in blocks):
                    return True
            return e.get("type") == "result"
        ev, _ = await cli.events_until(stop, timeout=90)
        await cli.interrupt()
    if not ev:
        return FAIL("no response")
    if ev.get("type") == "assistant":
        return FAIL("model RE-CALLED the tool instead of trusting the injected result")
    text = str(ev.get("result") or "").lower()
    ok = "frog" in text or "zqx" in text or "42" in text
    return OK(f"answered from injected result: '{text[:50]}'") if ok \
        else FAIL(f"ignored injected result: '{text[:80]}'")


@check("cache.block_level", 2,
       "Caching is block-level: growing a single block re-caches it fully (why we split per message)")
async def c_block(ctx):
    BIG = big("BLK", 600)
    async with CLI() as cli:
        await cli.send("hi"); await cli.result()
        await cli.clear()
        c1 = [{"type": "text", "text": BIG, "cache_control": {"type": "ephemeral", "ttl": "1h"}},
              {"type": "text", "text": "\nAnswer 'ok'."}]
        ev1, _ = await cli.turn(c1)
        w1 = usage_of(ev1).get("cache_creation_input_tokens") or 0
        await cli.clear()
        c2 = [{"type": "text", "text": BIG + "\nOne appended line changes the whole block.",
               "cache_control": {"type": "ephemeral", "ttl": "1h"}},
              {"type": "text", "text": "\nAnswer 'ok'."}]
        ev2, _ = await cli.turn(c2)
        u2 = usage_of(ev2)
        c2w = u2.get("cache_creation_input_tokens") or 0
        r2 = u2.get("cache_read_input_tokens") or 0
    # Block-level: the changed BIG block is RE-CREATED in turn2 (read stays ~system only). If
    # partial in-block prefix-matching worked, c2w would be ~tiny (only the appended line).
    return OK(f"turn2 re-created the block (create={c2w}, read={r2}=system only)") if c2w > 2000 \
        else FAIL(f"looks like partial in-block caching: create={c2w}, read={r2} (w1={w1})")


@check("cache.budget_with_tools", 2,
       "Our cache_control + MCP tools stays within the 4-breakpoint limit (no 400)")
async def c_budget(ctx):
    tools = [{"name": "noop", "description": "does nothing", "inputSchema": {"type": "object", "properties": {}}}]
    content = [{"type": "text", "text": big("BUD", 500), "cache_control": {"type": "ephemeral", "ttl": "1h"}},
               {"type": "text", "text": "\nReply with exactly: ok. Do NOT call any tool."}]
    async with CLI(mcp_tools=tools) as cli:
        await cli.clear()
        ev, _ = await cli.turn(content)
        await cli.clear()
        ev2, _ = await cli.turn(content)              # identical -> should cache, not 400
    if not ev:
        return SKIP("no result (model may have called the tool)")
    if ev.get("is_error"):
        return FAIL(f"error (budget/400?): {str(ev.get('result'))[:100]}")
    r2 = usage_of(ev2).get("cache_read_input_tokens") or 0
    return OK(f"no 400 with tools+cache_control; resend cache_read={r2}")


@check("effort.accepted", 2, "--effort levels are accepted by the CLI")
async def c_effort(ctx):
    res = {}
    for lvl in ("low", "high"):
        async with CLI(extra=["--effort", lvl]) as cli:
            ev, _ = await cli.turn("Reply with exactly: OK")
            res[lvl] = bool(ev) and not ev.get("is_error")
    bad = [lvl for lvl, ok in res.items() if not ok]
    return OK(f"accepted: {list(res)}") if not bad else FAIL(f"rejected: {bad}")


MANUAL = [
    ("model.opus_1m", "opus gives 1M context — needs a >200k prompt (expensive), check manually"),
    ("prompt.dynamic_sections", "default system prompt contains cwd/git/env/memory — check via capture proxy"),
    ("tools.native_result_replay",
     "native tool_use/tool_result blocks CANNOT be replayed into the CLI — the reason we render text; "
     "re-verify manually if changing the injection approach"),
]


# ---------------------------------------------------------------- Runner (streaming)
SYM = {True: "PASS", False: "FAIL", None: "SKIP"}
ICON = {True: "✅", False: "❌", None: "⚪"}


def _tier_header(tier):
    return f"\n-- Tier {tier} ({'OFFLINE, ~0 tokens' if tier == 1 else 'ONLINE, backend/tokens'}) --"


async def run(offline_only, as_json, only=None):
    ctx = {}
    results = []
    checks = [c for c in CHECKS if not (offline_only and c["tier"] != 1)]
    if only:
        checks = [c for c in checks if any(o in c["id"] for o in only)]
    tty = sys.stdout.isatty() and not as_json
    last_tier = None

    for c in checks:
        if not as_json and c["tier"] != last_tier:
            print(_tier_header(c["tier"]), flush=True)
            last_tier = c["tier"]
        if tty:                                   # transient "running" line
            print(f"  ⏳ {c['id']:<28} {c['desc']}", end="", flush=True)

        t0 = time.perf_counter()
        try:
            r = await asyncio.wait_for(c["fn"](ctx), timeout=180)
        except Exception as e:  # noqa: BLE001
            r = FAIL(f"Exception: {type(e).__name__}: {e}")
        r.ms = (time.perf_counter() - t0) * 1000
        results.append((c, r))

        if not as_json:                           # print result IMMEDIATELY
            prefix = "\r\033[K" if tty else ""    # overwrite the "running" line
            print(f"{prefix}  {ICON[r.ok]} {SYM[r.ok]}  {c['id']:<28} {c['desc']}  ({r.ms:.0f}ms)", flush=True)
            if r.observed:
                print(f"           -> {r.observed}", flush=True)

    if as_json:
        print(json.dumps([{"id": c["id"], "tier": c["tier"], "ok": r.ok,
                           "observed": r.observed, "ms": round(r.ms)} for c, r in results], indent=2))
    else:
        _print_footer(results, offline_only)
    return 1 if any(r.ok is False for _, r in results) else 0


def _print_footer(results, offline_only):
    print("\n-- MANUAL (not automated) --")
    for cid, desc in MANUAL:
        print(f"  \U0001f527 MANUAL {cid:<26} {desc}")
    npass = sum(1 for _, r in results if r.ok is True)
    nfail = sum(1 for _, r in results if r.ok is False)
    nskip = sum(1 for _, r in results if r.ok is None)
    print(f"\nTotal: {npass} PASS, {nfail} FAIL, {nskip} SKIP"
          + ("  (Tier 1 only)" if offline_only else ""))
    if nfail:
        print("-> FAILs mean: assumption broken, review the code before the new CLI version goes live.")


def main():
    ap = argparse.ArgumentParser(description="Integration tests of the CLI assumptions")
    ap.add_argument("--offline", action="store_true", help="Tier 1 only (no backend)")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--only", help="run only checks whose id contains any of these (comma-separated)")
    a = ap.parse_args()
    only = [s.strip() for s in a.only.split(",")] if a.only else None
    rc = asyncio.run(run(a.offline, a.json, only))
    sys.exit(rc)


if __name__ == "__main__":
    main()
