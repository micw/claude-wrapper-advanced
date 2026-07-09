"""Treibt die Claude Code CLI. Dispatcht auf Pool (Reuse) oder One-Shot.

Events (async-generator), einheitlich für beide Modi:
  ("delta", text)          - Text-Token
  ("tool_use", blocks)     - nativer Tool-Call
  ("result", text)         - finale Antwort
  ("error", {type,message})- Timeout / CLI-Fehler / unerwartetes Ende

`stats` wird befüllt: spawn_ms, ttft_ms, outcome, reused, usage, cost_usd,
stop_reason, cli_duration_ms, cli_ttft_ms, num_turns, stderr_tail.
"""
import asyncio
import contextlib
import json
import logging
import sys
import time
from collections import deque
from pathlib import Path

from .config import settings
from .metrics import metrics

log = logging.getLogger("cli")
_MCP_SERVER = str(Path(__file__).parent / "mcp_tool_server.py")


def _build_args(mcp_tools, model, effort=None):
    args = [
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--no-session-persistence",
        "--tools", "",  # alle Built-in-Tools aus
    ]
    eff = effort or settings.effort           # per-Request > Env-Default
    if eff:                                    # Latenz-Hebel: low|medium|high|xhigh|max
        args += ["--effort", eff]
    if settings.system_prompt:                # Token-Hebel: ersetzt den ~6.5k Default-Prompt
        args += ["--system-prompt", settings.system_prompt]
    if mcp_tools:
        mcp_config = {
            "mcpServers": {
                "t": {
                    "command": sys.executable,
                    "args": [_MCP_SERVER],
                    "env": {"TOOLS_JSON": json.dumps(mcp_tools)},
                }
            }
        }
        allowed = " ".join(f"mcp__t__{t['name']}" for t in mcp_tools if t.get("name"))
        args += [
            "--strict-mcp-config",
            "--mcp-config", json.dumps(mcp_config),
            "--allowedTools", allowed,               # Konzept-Alignment (§6)
            "--dangerously-skip-permissions",        # MCP-Tools headless ohne Prompt
        ]
    args += ["--model", model]
    return args


def _usage_obj(u):
    u = u or {}
    inp = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
        + (u.get("cache_creation_input_tokens") or 0)
    out = u.get("output_tokens") or 0
    return {
        "prompt_tokens": inp,
        "completion_tokens": out,
        "total_tokens": inp + out,
        "prompt_tokens_details": {
            "cached_tokens": u.get("cache_read_input_tokens") or 0,             # OpenAI-Standard (Cache-Hits)
            "cache_creation_tokens": u.get("cache_creation_input_tokens") or 0,  # Extension (Cache-Writes)
        },
    }


def _capture_result(m, stats):
    stats["usage"] = _usage_obj(m.get("usage"))
    stats["cost_usd"] = m.get("total_cost_usd")
    stats["stop_reason"] = m.get("stop_reason")
    stats["cli_duration_ms"] = m.get("duration_ms")
    stats["cli_ttft_ms"] = m.get("ttft_ms")
    stats["num_turns"] = m.get("num_turns")


def classify(m, stats, mark_ttft):
    """Eine stream-json-Zeile -> Event-Tupel oder None. Befüllt stats."""
    t = m.get("type")
    if t == "rate_limit_event":
        info = m.get("rate_limit_info") or {}
        metrics.update_rate_limit(info)
        if info.get("status") and info.get("status") != "allowed":
            log.warning("rate limit status=%s type=%s resetsAt=%s",
                        info.get("status"), info.get("rateLimitType"), info.get("resetsAt"))
        return None
    if t == "stream_event":
        ev = m.get("event") or {}
        if ev.get("type") == "content_block_delta":
            d = ev.get("delta") or {}
            if d.get("type") == "text_delta" and d.get("text"):
                mark_ttft()
                return ("delta", d["text"])
        return None
    if t == "assistant":
        blocks = (m.get("message") or {}).get("content") or []
        tus = [b for b in blocks if b.get("type") == "tool_use"]
        if tus:
            mark_ttft()
            # Bei Tool-Calls kommt kein result-Event -> Usage aus der assistant-Message.
            msg = m.get("message") or {}
            if msg.get("usage"):
                stats["usage"] = _usage_obj(msg["usage"])
            stats["outcome"] = "tool_call"
            return ("tool_use", tus)
        return None
    if t == "result":
        mark_ttft()
        _capture_result(m, stats)
        if m.get("is_error"):
            stats["outcome"] = "error"
            return ("error", {"type": "cli_error",
                              "message": (m.get("result") or m.get("subtype") or "cli error")})
        stats["outcome"] = "final"
        return ("result", m.get("result") or "")
    return None


def _timeout_evt():
    return ("error", {"type": "timeout",
                      "message": f"CLI timeout after {settings.request_timeout:.0f}s"})


async def _oneshot_turn(prompt, mcp_tools, model, stats, effort=None):
    """Eine frische CLI pro Request (kein Reuse)."""
    t0 = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        settings.claude_bin, *_build_args(mcp_tools, model, effort),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=settings.workdir,
    )
    stats["spawn_ms"] = (time.perf_counter() - t0) * 1000
    stats["reused"] = False

    stderr_tail = deque(maxlen=50)

    async def _drain_err():
        with contextlib.suppress(Exception):
            async for line in proc.stderr:
                s = line.decode(errors="replace").rstrip()
                if s:
                    stderr_tail.append(s)

    err_task = asyncio.create_task(_drain_err())

    msg = json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n"
    proc.stdin.write(msg.encode())
    with contextlib.suppress(Exception):
        await proc.stdin.drain()

    first = [False]

    def mark():
        if not first[0]:
            stats["ttft_ms"] = (time.perf_counter() - t0) * 1000
            first[0] = True

    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.request_timeout
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                stats["outcome"] = "timeout"
                yield _timeout_evt()
                return
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                stats["outcome"] = "timeout"
                yield _timeout_evt()
                return
            if not raw:
                break  # EOF
            try:
                m = json.loads(raw)
            except Exception:
                continue
            ev = classify(m, stats, mark)
            if ev is None:
                continue
            yield ev
            if ev[0] in ("tool_use", "result", "error"):
                return

        # EOF ohne terminales Event -> CLI ist abgestürzt.
        tail = "\n".join(stderr_tail)
        stats["outcome"] = "error"
        stats["stderr_tail"] = tail
        log.error("CLI beendet ohne result. stderr:\n%s", tail or "(leer)")
        yield ("error", {"type": "cli_exit",
                         "message": (tail[-500:] if tail else "CLI exited without producing a result")})
    finally:
        err_task.cancel()
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        if settings.log_cli_stderr and stderr_tail:
            log.debug("CLI stderr tail:\n%s", "\n".join(stderr_tail))


async def drive_turn(prompt, mcp_tools, model, stats, effort=None):
    """Öffentliche Schnittstelle: Pool (Reuse) oder One-Shot je nach Config."""
    if settings.pool_enabled:
        from .pool import pooled_drive_turn  # lazy: vermeidet Zirkularimport
        async for ev in pooled_drive_turn(prompt, mcp_tools, model, stats, effort):
            yield ev
    else:
        async for ev in _oneshot_turn(prompt, mcp_tools, model, stats, effort):
            yield ev
