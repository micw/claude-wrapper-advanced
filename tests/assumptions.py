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
import contextlib
import json
import os
import sys
import time
from pathlib import Path

# Salt filler text per run so the 1h prompt cache from a previous run never pollutes cache tests.
NONCE = f"{int(time.time())}-{os.getpid()}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings                     # noqa: E402
from app import limits                              # noqa: E402
from app.cli_driver import _PARENT_SESSION_VARS, _build_args, child_env  # noqa: E402
from app.turn_headers import Capture                 # noqa: E402
from app.translate import messages_to_prompt, openai_tools_to_mcp  # noqa: E402

CLAUDE = settings.claude_bin

# Flags/subcommands the proxy relies on -> must still exist in --help (drift early-warning).
REQUIRED_FLAGS = [
    "--input-format", "--output-format", "--verbose", "--include-partial-messages",
    "--no-session-persistence", "--tools", "--effort",
    "--system-prompt", "--append-system-prompt",
    "--strict-mcp-config", "--mcp-config", "--allowedTools",
    "--dangerously-skip-permissions", "--model", "--safe-mode",
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
        # child_env() wie im Proxy — sonst verfälscht eine ELTERN-Claude-Code-Session die
        # Cache-Messungen (siehe env.no_parent_session).
        self.proc = await asyncio.create_subprocess_exec(
            CLAUDE, *self.args, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=child_env())
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


async def minimal_usage_probe(model):
    """The production probe shape, with its result retained for magnitude assertions."""
    from app.cli_driver import spawn_cli
    limits._reset_observations()
    args = ["-p", "Reply only: OK", "--model", model, "--system-prompt", "",
            "--safe-mode", "--tools", "", "--output-format", "json",
            "--no-session-persistence"]
    proc = await spawn_cli(args, model, stdin=asyncio.subprocess.DEVNULL)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=45)
    capture = getattr(proc, "turn_header_capture", None)
    if capture is not None:
        await capture.close()
    if proc.returncode != 0:
        raise RuntimeError((err or b"")[-300:].decode(errors="replace"))
    return json.loads(out), limits.quota_snapshot()


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


@check("sysprompt.replace_and_append_stack", 1,
       "--system-prompt (base) and --append-system-prompt (client) both reach the API and stack")
async def c_sysprompt_stack(ctx):
    # Lokales Fake-Backend fängt den Request-Body ab und antwortet 400 (die CLI gibt sofort auf).
    # Kein Token-Verbrauch, daher Tier 1. Der Replace-Modus setzt darauf, dass --system-prompt den
    # Default ersetzt UND ein --append-system-prompt zusätzlich ankommt (Client gewinnt bei Konflikt).
    # Bricht das (nur eins der beiden kommt an), verschwindet still Basis oder Client.
    import http.server
    import threading

    captured = []

    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            with contextlib.suppress(Exception):
                captured.append(json.loads(self.rfile.read(n)))
            body = b'{"type":"error","error":{"type":"invalid_request_error","message":"x"}}'
            self.send_response(400)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            # Synthetic quota values: proves the compiled CLI used our preloaded fetch and
            # that response headers crossed the dedicated fd. Still no backend/tokens.
            self.send_header("anthropic-ratelimit-unified-5h-utilization", "0.12")
            self.send_header("anthropic-ratelimit-unified-5h-reset", "1788278400")
            self.send_header("anthropic-ratelimit-unified-7d-utilization", "0.34")
            self.send_header("anthropic-ratelimit-unified-7d-reset", "1788631200")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    limits._reset_observations()
    capture = Capture()
    env = capture.configure({**child_env(), "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                             "ANTHROPIC_API_KEY": "sk-assumptions-dummy"})
    args = [CLAUDE, "-p", "hi", "--no-session-persistence", "--output-format", "stream-json",
            "--verbose", "--tools", "", "--model", "sonnet",
            "--system-prompt", "MARKER_BASE_xyz",
            "--append-system-prompt", "MARKER_CLIENT_xyz"]
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.DEVNULL, env=env,
                                                pass_fds=capture.pass_fds)
    await capture.parent_started()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=30)
    with contextlib.suppress(Exception):
        proc.kill()
    await capture.close()
    srv.shutdown()
    quota = limits.quota_snapshot()
    ctx["preload_capture"] = {
        "ready": capture.ready,
        "global": quota["groups"][0],
    }
    if not captured:
        return SKIP("kein Request abgefangen (Base-URL nicht benutzt?)")
    blob = "\n".join(b.get("text", "") for b in (captured[0].get("system") or []))
    base, client = "MARKER_BASE_xyz" in blob, "MARKER_CLIENT_xyz" in blob
    if base and client:
        return OK("beide Flags stapeln")
    return FAIL(f"Stacking verletzt: base={base} client={client} — _build_args prüfen")


@check("capture.preload_headers", 1,
       "Bun preload sees /v1/messages response headers in the compiled CLI (offline)")
async def c_capture_preload(ctx):
    observed = ctx.get("preload_capture") or {}
    global_ = observed.get("global") or {}
    windows = {w.get("id"): w for w in global_.get("windows") or []}
    if not observed.get("ready"):
        return FAIL("preload_ready missing — BUN_OPTIONS/preload changed")
    if windows.get("five_hour", {}).get("used_percent") != 12:
        return FAIL(f"synthetic 5h header missing: {windows}")
    if windows.get("seven_day", {}).get("used_percent") != 34:
        return FAIL(f"synthetic 7d header missing: {windows}")
    return OK("preload + fetch hook + response-header fd work")


# ================================================================ TIER 2: ONLINE
@check("env.no_parent_session", 1, "Parent Claude Code session markers are stripped from the CLI env")
async def c_childenv(ctx):
    """Why this matters (cost us a bogus 'CLI regression' once): if the proxy runs inside a
    Claude Code session, the CLI inherits CLAUDE_CODE_ENTRYPOINT — and since 2.1.198 it then adds
    a scratchpad section WITH A SESSION UUID to its system prompt. Every /clear mints a new UUID,
    so the cached prefix never matches and the whole history is re-written each turn
    (measured: 100% cache_read -> 0%). child_env() removes those markers.
    """
    leaked = [v for v in _PARENT_SESSION_VARS if v in child_env()]
    if leaked:
        return FAIL(f"still in the child env: {leaked}")
    if "CLAUDE_CODE_OAUTH_TOKEN" in os.environ and "CLAUDE_CODE_OAUTH_TOKEN" not in child_env():
        return FAIL("child_env() dropped CLAUDE_CODE_OAUTH_TOKEN (auth would break)")
    present = [v for v in _PARENT_SESSION_VARS if v in os.environ]
    return OK(f"stripped: {present or 'none set here'}")


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


def probe_magnitude(result, quota, model, output_max, cost_min, cost_max):
    usage = result.get("usage") or {}
    inp = ((usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
           + (usage.get("cache_creation_input_tokens") or 0))
    out = usage.get("output_tokens") or 0
    cache = (usage.get("cache_read_input_tokens") or 0) \
        + (usage.get("cache_creation_input_tokens") or 0)
    cost = result.get("total_cost_usd")
    groups = {g["id"]: g for g in quota["groups"]}
    global_ok = groups["global"]["observed_at"] is not None
    fable_ok = groups["model:fable-5"]["observed_at"] is not None
    detail = f"input={inp} output={out} cache={cache} cost={cost} global={global_ok} fable={fable_ok}"
    if not 50 <= inp <= 500:
        return FAIL(f"probe input magnitude drifted: {detail}")
    if not 1 <= out <= output_max:
        return FAIL(f"probe output magnitude drifted: {detail}")
    if cache != 0:
        return FAIL(f"minimal probe unexpectedly cached: {detail}")
    if not isinstance(cost, (int, float)) or not cost_min <= cost <= cost_max:
        return FAIL(f"probe nominal cost magnitude drifted: {detail}")
    if not global_ok or (model.startswith("fable-") and not fable_ok):
        return FAIL(f"probe quota headers missing: {detail}")
    return OK(detail)


@check("usage.probe_haiku_magnitude", 2,
       "Minimal Haiku quota probe stays small and yields global headers")
async def c_probe_haiku(ctx):
    result, quota = await minimal_usage_probe(settings.models["haiku-4-5"][0])
    return probe_magnitude(result, quota, "haiku-4-5", 300, 0.0001, 0.01)


@check("usage.probe_fable_magnitude", 2,
       "Minimal Fable quota probe stays small and yields scoped headers")
async def c_probe_fable(ctx):
    result, quota = await minimal_usage_probe(settings.models["fable-5-1"][0])
    return probe_magnitude(result, quota, "fable-5-1", 100, 0.0002, 0.02)


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


@check("cost.cumulative", 2,
       "total_cost_usd is cumulative per process and survives /clear (pool computes per-turn deltas)")
async def c_cost_cumulative(ctx):
    # pool.Proc rechnet total_cost_usd auf ein Per-Turn-Delta um. Wäre der Wert per Turn (oder
    # würde er bei /clear zurückgesetzt), lieferte die Subtraktion still 0.0 für jeden Turn.
    async with CLI() as cli:
        ev1, _ = await cli.turn("Say hi")
        ev2, _ = await cli.turn("Say hi again")
    c1 = ev1.get("total_cost_usd") if ev1 else None
    c2 = ev2.get("total_cost_usd") if ev2 else None
    if c1 is None or c2 is None:
        return SKIP(f"no cost in result (c1={c1} c2={c2})")
    if c2 > c1:
        return OK(f"cumulative: turn1={c1:.6f} -> turn2={c2:.6f} (delta={c2 - c1:.6f})")
    return FAIL(f"NOT cumulative: turn1={c1:.6f} turn2={c2:.6f} — pool delta would report 0.0")


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


@check("model.registry", 2, "Every model name we advertise in /v1/models is accepted by the CLI")
async def c_models(ctx):
    # Wir reichen nur volle Namen an die CLI (nie Aliase — die driften mit der CLI-Version).
    # Ein CLI-Update, das einen Namen fallen lässt, muss hier auffallen und nicht im Betrieb.
    res = {}
    for cli_model, _name, _ctx, _levels, _cutoff, _modalities in settings.models.values():
        async with CLI(model=cli_model) as cli:
            ev, _ = await cli.turn("Reply with exactly: OK")
            res[cli_model] = bool(ev) and not ev.get("is_error")
    bad = [m for m, ok in res.items() if not ok]
    return OK(f"accepted: {len(res)}") if not bad else FAIL(f"rejected: {bad}")


@check("model.unknown_is_404", 2,
       "Unknown model fails with api_error_status 404 (that is what we translate into our 404)")
async def c_model_404(ctx):
    async with CLI(model="claude-erfunden-9") as cli:
        ev, _ = await cli.turn("Reply with exactly: OK")
    if not ev or not ev.get("is_error"):
        return FAIL(f"expected an error result, got {ev and ev.get('subtype')}")
    st = ev.get("api_error_status")
    # 'subtype' steht dabei auf "success" — nur is_error/api_error_status sind belastbar.
    return OK(f"api_error_status={st}") if st == 404 else \
        FAIL(f"api_error_status={st!r} (erwartet 404) — _upstream_code() prüfen")


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


# 64x64 PNG, linke Hälfte rot / rechte Hälfte blau — klein genug für den Quelltext.
def _test_png_b64():
    import base64
    import struct
    import zlib
    w = h = 64
    raw = b"".join(b"\x00" + b"".join(b"\xff\x00\x00" if x < w // 2 else b"\x00\x00\xff"
                                     for x in range(w)) for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return base64.b64encode(png).decode()


@check("vision.image_block", 2, "stream-json accepts base64 image blocks (Vision works at all)")
async def c_image(ctx):
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": _test_png_b64()}},
        {"type": "text", "text": "Name the two colors in the image. Answer with the two words only."},
    ]
    async with CLI() as cli:
        ev, _ = await cli.turn(content)
    if not ev:
        return FAIL("no result event")
    if ev.get("is_error"):
        return FAIL(f"CLI/API rejected the image block: {str(ev.get('result'))[:120]}")
    txt = (ev.get("result") or "").lower()
    ctx["image_seen"] = "red" in txt and "blue" in txt
    return (OK(f"image understood: {txt[:60]!r}") if ctx["image_seen"]
            else FAIL(f"accepted but colors not recognized: {txt[:80]!r}"))


@check("vision.image_in_history", 2, "image block in an EARLIER message stays visible (multi-turn)")
async def c_image_history(ctx):
    if not ctx.get("image_seen"):
        return SKIP("vision.image_block failed — nothing to build on")
    # Wie translate.messages_to_prompt rendert: Bild VOR dem Text seiner Nachricht.
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": _test_png_b64()}},
        {"type": "text", "text": "User: What is in the image?\n"},
        {"type": "text", "text": "Assistant: A graphic with two color areas.\n"},
        {"type": "text", "text": "User: Which color is on the LEFT half? One word only.\n"},
    ]
    async with CLI() as cli:
        ev, _ = await cli.turn(content)
    if not ev or ev.get("is_error"):
        return FAIL(f"no/err result: {str((ev or {}).get('result'))[:120]}")
    txt = (ev.get("result") or "").lower()
    return OK(f"left half recognized: {txt[:60]!r}") if "red" in txt else FAIL(f"got {txt[:80]!r}")


@check("vision.openai_payload", 2, "OpenAI image_url payload survives messages_to_prompt -> CLI")
async def c_image_payload(ctx):
    """The end-to-end path a pasted screenshot actually takes (this is what broke originally:
    _text() dropped image parts, so only text reached the CLI).

    Deliberately NOT gated on ctx: this is the regression test for our own rendering, it must
    fail loudly on its own rather than skip when the suite is run filtered.
    """
    messages = [{"role": "user", "content": [                    # exactly what Open WebUI sends
        {"type": "text", "text": "Name the two colors in the image. Answer with the two words only."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _test_png_b64()}},
    ]}]
    prompt = messages_to_prompt(messages)
    if not isinstance(prompt, list) or not any(b.get("type") == "image" for b in prompt):
        return FAIL("messages_to_prompt produced no image block")
    async with CLI() as cli:
        ev, _ = await cli.turn(prompt)
    if not ev or ev.get("is_error"):
        return FAIL(f"no/err result: {str((ev or {}).get('result'))[:120]}")
    txt = (ev.get("result") or "").lower()
    return (OK(f"end-to-end: {txt[:60]!r}") if "red" in txt and "blue" in txt
            else FAIL(f"image did not reach the model: {txt[:80]!r}"))


@check("vision.dropped_image_no_error", 2, "An unsupported image (remote URL) must NOT fail the turn")
async def c_image_dropped(ctx):
    """Remote URLs are deliberately unsupported. The turn must still complete — a 400 from a
    passed-through url-source would kill the whole request instead of just the image."""
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "Reply with exactly: OK"},
        {"type": "image_url", "image_url": {"url": "https://example.com/nope.png"}},
    ]}]
    prompt = messages_to_prompt(messages)
    if any(b.get("type") == "image" for b in prompt if isinstance(b, dict)):
        return FAIL("remote URL was passed through as an image block")
    if "could not be passed through" not in json.dumps(prompt):
        return FAIL("no note about the dropped image reached the prompt")
    async with CLI() as cli:
        ev, _ = await cli.turn(prompt)
    if not ev:
        return FAIL("no result event")
    return (FAIL(f"turn errored: {str(ev.get('result'))[:120]}") if ev.get("is_error")
            else OK(f"turn completed, image dropped with a note: {str(ev.get('result'))[:40]!r}"))


@check("cli.streams_continuously", 2, "CLI never goes silent mid-turn (the basis for IDLE_TIMEOUT)")
async def c_streams(ctx):
    """Our timeout is an idle window, not a total deadline — that only holds if the CLI keeps
    emitting while it works. It does: thinking_deltas flow while the model reasons (they carry
    no text, only estimated_tokens, but they keep the stream alive). Measured worst case is the
    prefill gap: ~10s at 1MB of context, <2s otherwise.
    """
    async with CLI(model="opus", extra=["--effort", "xhigh"]) as cli:
        await cli.send("Prove rigorously whether 1729 is the smallest number expressible as a sum "
                       "of two positive cubes in two different ways. Verify alternatives carefully.")
        loop = asyncio.get_event_loop()
        last, maxgap, kinds = loop.time(), 0.0, set()
        while True:
            try:
                raw = await asyncio.wait_for(cli.proc.stdout.readline(), timeout=180)
            except asyncio.TimeoutError:
                return FAIL("no line for 180s")
            if not raw:
                return FAIL("stream ended without a result")
            now = loop.time()
            maxgap = max(maxgap, now - last)
            last = now
            try:
                m = json.loads(raw)
            except Exception:
                continue
            d = ((m.get("event") or {}).get("delta") or {})
            if d.get("type"):
                kinds.add(d["type"])
            if m.get("type") == "result":
                break
    ctx["max_gap_s"] = maxgap
    thinking = "thinking_delta" in kinds
    if maxgap > settings.idle_timeout / 2:
        return FAIL(f"largest silence {maxgap:.1f}s — too close to IDLE_TIMEOUT="
                    f"{settings.idle_timeout:.0f}s, raise it")
    return OK(f"largest silence {maxgap:.1f}s (IDLE_TIMEOUT={settings.idle_timeout:.0f}s), "
              f"thinking_delta={'yes' if thinking else 'no'}")


@check("cli.thinking_is_redacted", 2, "thinking_deltas carry NO text (why we cannot forward reasoning)")
async def c_thinking(ctx):
    """If this ever starts carrying text, we can forward it as reasoning_content and the long
    silent wait before the first token becomes visible to the user."""
    async with CLI(model="opus", extra=["--effort", "xhigh"]) as cli:
        await cli.send("Think hard, then answer: is 8191 prime? Verify by trial division.")
        seen, texty = 0, False
        while seen < 5:
            try:
                raw = await asyncio.wait_for(cli.proc.stdout.readline(), timeout=180)
            except asyncio.TimeoutError:
                break
            if not raw:
                break
            try:
                m = json.loads(raw)
            except Exception:
                continue
            d = ((m.get("event") or {}).get("delta") or {})
            if d.get("type") == "thinking_delta":
                seen += 1
                if (d.get("thinking") or "").strip():
                    texty = True
            if m.get("type") == "result":
                break
    if not seen:
        return SKIP("no thinking_delta observed in this turn")
    return (FAIL(f"thinking now carries text ({seen} deltas) — reasoning_content is possible, "
                 "revisit the README limitation") if texty
            else OK(f"{seen} thinking_deltas, all empty (progress signal only)"))


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
