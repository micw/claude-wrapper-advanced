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
import os
import sys
import time
from collections import deque
from pathlib import Path

from .config import settings
from .metrics import metrics

log = logging.getLogger("cli")
_MCP_SERVER = str(Path(__file__).parent / "mcp_tool_server.py")


# Marker einer ELTERN-Claude-Code-Session. Wird der Proxy aus einem Claude-Code-Terminal
# heraus gestartet, erbt die CLI sie — und ab 2.1.198 hängt sie dann einen Scratchpad-Abschnitt
# MIT SESSION-UUID in ihren System-Prompt. Jedes /clear vergibt eine neue UUID -> der System-Block
# ändert sich pro Turn -> der cache_control-Prefix trifft nie, die History wird jedes Mal neu
# geschrieben (empirisch: 100% cache_read -> 0%). Deshalb beim Spawn entfernen.
# NICHT entfernen: CLAUDE_CODE_OAUTH_TOKEN (Auth) und ANTHROPIC_* (Endpoint/Proxy).
_PARENT_SESSION_VARS = (
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_EXECPATH", "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT",
)


def child_env():
    """Env für die CLI: Eltern-Session-Marker raus (siehe _PARENT_SESSION_VARS)."""
    env = {k: v for k, v in os.environ.items() if k not in _PARENT_SESSION_VARS}
    if len(env) != len(os.environ):
        log.debug("Eltern-Session-Marker aus dem CLI-Env entfernt (Prompt-Cache-Schutz)")
    return env


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
            if d.get("type") == "thinking_delta":
                # KEIN mark_ttft(): ttft bleibt "erstes Text-Token", sonst sind die Latenz-
                # Metriken nicht mehr mit früheren Läufen vergleichbar.
                est = d.get("estimated_tokens") or 0
                # Hier mitzählen (nicht erst im Endpunkt): so steht die Zahl beiden Endpunkten
                # und auch dem Non-Streaming-Pfad zur Verfügung, unabhängig von STREAM_THINKING.
                stats["thinking_tokens"] = stats.get("thinking_tokens", 0) + est
                return ("thinking", est)
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
            # Achtung: 'subtype' steht auch im Fehlerfall auf "success" — nur is_error zählt.
            # api_error_status trägt den echten Upstream-Status (z.B. 404 bei Modellfehlern).
            return ("error", {"type": "cli_error",
                              "message": (m.get("result") or m.get("subtype") or "cli error"),
                              "status": m.get("api_error_status")})
        stats["outcome"] = "final"
        return ("result", m.get("result") or "")
    return None


class Silent(asyncio.TimeoutError):
    """Stille > idle_timeout — im Gegensatz zum Erreichen des Gesamt-Deckels."""


async def read_line(stdout, hard_deadline, loop):
    """Nächste Stream-Zeile lesen. Silent bei Stille, TimeoutError am Gesamt-Deckel.

    Idle statt Gesamtfrist: eine laufende Antwort darf dauern (opus/xhigh denkt minutenlang und
    schickt dabei nur inhaltsleere thinking_deltas), ein hängender Prozess fällt trotzdem nach
    idle_timeout auf — schneller als die alte 180s-Gesamtfrist, die stattdessen ARBEITENDE
    Turns abgeschnitten hat.
    """
    remaining = hard_deadline - loop.time()
    if remaining <= 0:
        raise asyncio.TimeoutError
    try:
        return await asyncio.wait_for(stdout.readline(),
                                      timeout=min(settings.idle_timeout, remaining))
    except asyncio.TimeoutError:
        if loop.time() >= hard_deadline:
            raise
        raise Silent from None


def _timeout_evt(silent=True):
    msg = (f"CLI sent nothing for {settings.idle_timeout:.0f}s" if silent
           else f"CLI turn exceeded {settings.request_timeout:.0f}s")
    return ("error", {"type": "timeout", "message": msg})


async def _oneshot_turn(prompt, mcp_tools, model, stats, effort=None):
    """Eine frische CLI pro Request (kein Reuse)."""
    t0 = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        settings.claude_bin, *_build_args(mcp_tools, model, effort),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=settings.workdir,
        env=child_env(),
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
            try:
                raw = await read_line(proc.stdout, deadline, loop)
            except asyncio.TimeoutError as e:
                stats["outcome"] = "timeout"
                yield _timeout_evt(isinstance(e, Silent))
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
