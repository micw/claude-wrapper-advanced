"""Treibt die Claude Code CLI. Dispatcht auf Pool (Reuse) oder One-Shot.

Der Strom besteht aus Wire-Ereignissen (`app/wire.py`), einheitlich für beide Modi:
`Started`, `TextDelta`, `ThinkingProgress`, `ToolCall`, `LimitStatus`, `Done`, `Failed`.
Die Abschlussdaten — Usage, Kosten, Zeiten — stehen **im** `Done`-Ereignis.

`drive_turn_events()` liefert diesen Strom. `drive_turn()` daneben ist der Adapter auf
die alten Tupel, solange die OpenAI-Oberflächen in `main.py` noch daraus gebaut werden;
er entfällt mit deren Umstellung.

`stats` wird weiter befüllt (spawn_ms, ttft_ms, outcome, reused, usage, cost_usd,
stop_reason, cli_duration_ms, num_turns, stderr_tail) — der Pool braucht es für die
Kosten-Delta-Rechnung, und `/metrics` liest daraus. Neu ist, dass kein Konsument mehr
darauf angewiesen ist: was ein Turn abschließend zu sagen hat, steht im Ereignis.
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

from . import limits, wire
from .config import settings
from .metrics import metrics
from .translate import tooluse_to_toolcalls

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


def _build_args(mcp_tools, model, effort=None, system_prompt=None, append_system=None):
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
    # Beide Prompt-Strings werden übergeben (in main aus Registry+Config gebaut), nicht hier global
    # gelesen. system_prompt (Basis + per-Modell Identität/Cutoff) ERSETZT den CLI-Default via
    # --system-prompt; ist es None (REPLACE_SYSTEM_PROMPT=0), bleibt der Default stehen. Ein
    # Client-System-Prompt hängt IMMER via --append-system-prompt dahinter — die beiden Flags
    # stapeln (empirisch verifiziert), der Client gewinnt bei Konflikt.
    if system_prompt:
        args += ["--system-prompt", system_prompt]
    if append_system:
        args += ["--append-system-prompt", append_system]
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
            "cache_write_tokens": u.get("cache_creation_input_tokens") or 0,     # Extension (Cache-Writes)
        },
    }


def _capture_result(m, stats):
    stats["usage"] = _usage_obj(m.get("usage"))
    stats["usage_raw"] = m.get("usage") or {}
    stats["cost_usd"] = m.get("total_cost_usd")
    # Aufschlüsselung pro Modell. Enthält gemessen auch CLI-interne Nebenaufrufe
    # (Haiku), ist also der einzige Weg, die Kosten des Modell-Turns zu isolieren.
    stats["model_usage"] = m.get("modelUsage") or {}
    stats["stop_reason"] = m.get("stop_reason")
    stats["cli_duration_ms"] = m.get("duration_ms")
    stats["cli_ttft_ms"] = m.get("ttft_ms")
    stats["num_turns"] = m.get("num_turns")


def _done(stats, text):
    """Das Abschlussereignis aus dem, was der Turn gesammelt hat."""
    return wire.Done(
        stop_reason=stats.get("stop_reason"),
        text=text,
        usage=wire.usage(stats.get("usage_raw"), stats.get("thinking_tokens")),
        cost=wire.cost(stats.get("cost_usd"), stats.get("model_usage")),
        timing={
            "cli_ms": stats.get("cli_duration_ms"),
            "ttft_ms": stats.get("ttft_ms"),
            "spawn_ms": stats.get("spawn_ms"),
            "reused": stats.get("reused"),
        },
    )


def classify(m, stats, mark_ttft, model=None):
    """Eine stream-json-Zeile -> Liste von Wire-Ereignissen (oft leer). Befüllt stats.

    Eine Liste, weil eine einzelne assistant-Zeile mehrere `tool_use`-Blöcke tragen kann.
    `model` wird für den Fensterschlüssel eines `LimitStatus` gebraucht — der Wrapper ist
    die einzige Stelle, die weiß, auf welchem Modell der Turn lief (MESSUNGEN.md §6).
    """
    t = m.get("type")
    if t == "rate_limit_event":
        info = m.get("rate_limit_info") or {}
        metrics.update_rate_limit(info)
        if info.get("status") and info.get("status") != "allowed":
            log.warning("rate limit status=%s type=%s resetsAt=%s",
                        info.get("status"), info.get("rateLimitType"), info.get("resetsAt"))
        # Nur wenn etwas anliegt: der Normalfall wäre ein Ereignis pro Turn ohne Inhalt.
        status = limits.status_from_event(info, model)
        return [wire.LimitStatus(**status)] if status else []
    if t == "stream_event":
        ev = m.get("event") or {}
        kind = ev.get("type")
        # Die Input-Seite steht schon vor dem ersten Token fest. Für Turns, die mit einem
        # Tool-Call enden, ist das die einzige vollständige Usage-Quelle — ein
        # result-Ereignis kommt dort nie.
        if kind == "message_start":
            stats.setdefault("usage_raw", (ev.get("message") or {}).get("usage") or {})
            return []
        # message_delta trägt die ECHTE Denk-Token-Zahl (output_tokens_details.thinking_tokens);
        # sie steht weder im result-Event noch in dessen usage. Gemessen: 490 echt gegen 450
        # aus der Schätzsumme unten — die Schätzung ist brauchbar, aber sie ist eine Schätzung.
        if kind == "message_delta":
            usage = ev.get("usage") or {}
            det = usage.get("output_tokens_details") or {}
            real = det.get("thinking_tokens")
            if real is not None:
                stats["thinking_tokens"] = real
                stats["thinking_tokens_source"] = "api"
            if usage:
                stats["usage_raw"] = {**(stats.get("usage_raw") or {}), **usage}
            return []
        if kind == "content_block_delta":
            d = ev.get("delta") or {}
            if d.get("type") == "text_delta" and d.get("text"):
                mark_ttft()
                return [wire.TextDelta(text=d["text"])]
            if d.get("type") == "thinking_delta":
                # KEIN mark_ttft(): ttft bleibt "erstes Text-Token", sonst sind die Latenz-
                # Metriken nicht mehr mit früheren Läufen vergleichbar.
                est = d.get("estimated_tokens") or 0
                # Hier mitzählen (nicht erst im Endpunkt): so steht die Zahl beiden Endpunkten
                # und auch dem Non-Streaming-Pfad zur Verfügung, unabhängig von STREAM_THINKING.
                acc = stats.get("thinking_tokens_estimated", 0) + est
                stats["thinking_tokens_estimated"] = acc
                # Fallback, solange die echte Zahl nicht da ist (message_delta kommt zum Schluss).
                # Ein abgebrochener Turn behält so wenigstens die Schätzung.
                if stats.get("thinking_tokens_source") != "api":
                    stats["thinking_tokens"] = acc
                return [wire.ThinkingProgress(tokens=est)]
        return []
    if t == "assistant":
        blocks = (m.get("message") or {}).get("content") or []
        tus = [b for b in blocks if b.get("type") == "tool_use"]
        if tus:
            mark_ttft()
            # Bei Tool-Calls kommt kein result-Event -> Usage aus der assistant-Message.
            msg = m.get("message") or {}
            if msg.get("usage"):
                stats["usage"] = _usage_obj(msg["usage"])
                stats["usage_raw"] = msg["usage"]
            stats["outcome"] = "tool_call"
            stats["stop_reason"] = "tool_use"
            # Normalisiert (mcp__t__-Präfix weg, Argumente als JSON-String) — dieselbe
            # Form, die die OpenAI-Oberflächen brauchen, hier einmal statt zweimal.
            calls = tooluse_to_toolcalls(tus)
            tool_events = [
                # Native Wire keeps the backend id. The OpenAI adapter below still creates
                # its own call id from `_raw`, so this does not alter the compatibility API.
                wire.ToolCall(id=raw.get("id") or call["id"], name=call["function"]["name"],
                              arguments=call["function"]["arguments"], _raw=raw)
                for call, raw in zip(calls, tus)
            ]
            # A tool call ends this CLI turn just as a result does. Keep all terminal data in
            # Done; in particular message_start/assistant usage must not disappear here.
            return [*tool_events, _done(stats, "")]
        return []
    if t == "result":
        mark_ttft()
        _capture_result(m, stats)
        if m.get("is_error"):
            stats["outcome"] = "error"
            # Achtung: 'subtype' steht auch im Fehlerfall auf "success" — nur is_error zählt.
            # api_error_status trägt den echten Upstream-Status (z.B. 404 bei Modellfehlern).
            return [wire.Failed(error_type="cli_error",
                                message=(m.get("result") or m.get("subtype") or "cli error"),
                                upstream_status=m.get("api_error_status"))]
        stats["outcome"] = "final"
        return [_done(stats, m.get("result") or "")]
    return []


async def spawn_cli(args):
    """Startet die CLI. EINZIGE Spawn-Stelle — One-Shot und Pool teilen sie sich."""
    return await asyncio.create_subprocess_exec(
        settings.claude_bin, *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=settings.workdir,
        env=child_env(),
        limit=settings.stream_limit,   # NICHT weglassen: siehe settings.stream_limit
    )


class Silent(asyncio.TimeoutError):
    """Stille > idle_timeout — im Gegensatz zum Erreichen des Gesamt-Deckels."""


class Overlong(Exception):
    """Zeile > stream_limit. Der Turn ist verloren — readline() verwirft dabei den Puffer."""


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
    except ValueError as e:
        # readline() fängt den LimitOverrunError selbst und wirft ValueError — ein
        # `except LimitOverrunError` würde also nichts fangen. Hier umtypisieren, sonst fliegt
        # es ungefangen bis in uvicorn ("Exception in ASGI application") und der Client sieht
        # einen abgeschnittenen Body statt eines Fehlers.
        raise Overlong(str(e)) from e


def _overlong_evt():
    return wire.Failed(error_type="overlong_line",
                       message=f"CLI emitted a line larger than STREAM_LIMIT "
                               f"({settings.stream_limit} bytes)")


def _timeout_evt(silent=True):
    msg = (f"CLI sent nothing for {settings.idle_timeout:.0f}s" if silent
           else f"CLI turn exceeded {settings.request_timeout:.0f}s")
    # retryable: eine Stille ist ein Zustand, kein Urteil über den Request.
    return wire.Failed(error_type="timeout", message=msg, retryable=True)


#: Ereignisse, nach denen der Turn vorbei ist.
TERMINAL = (wire.ToolCall, wire.Done, wire.Failed)


async def _oneshot_turn(prompt, mcp_tools, model, stats, effort=None, system_prompt=None,
                        append_system=None):
    """Eine frische CLI pro Request (kein Reuse)."""
    t0 = time.perf_counter()
    proc = await spawn_cli(_build_args(mcp_tools, model, effort, system_prompt, append_system))
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
    yield wire.Started(model=model, reused=False)
    try:
        while True:
            try:
                raw = await read_line(proc.stdout, deadline, loop)
            except asyncio.TimeoutError as e:
                stats["outcome"] = "timeout"
                yield _timeout_evt(isinstance(e, Silent))
                return
            except Overlong as e:
                stats["outcome"] = "error"
                log.error("CLI-Zeile über STREAM_LIMIT (%s) — Turn verloren: %s",
                          settings.stream_limit, e)
                yield _overlong_evt()
                return
            if not raw:
                break  # EOF
            try:
                m = json.loads(raw)
            except Exception:
                continue
            events = classify(m, stats, mark, model)
            for event in events:
                yield event
            if any(isinstance(e, TERMINAL) for e in events):
                return

        # EOF ohne terminales Event -> CLI ist abgestürzt.
        tail = "\n".join(stderr_tail)
        stats["outcome"] = "error"
        stats["stderr_tail"] = tail
        log.error("CLI beendet ohne result. stderr:\n%s", tail or "(leer)")
        yield wire.Failed(error_type="cli_exit",
                          message=(tail[-500:] if tail
                                   else "CLI exited without producing a result"))
    finally:
        err_task.cancel()
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        if settings.log_cli_stderr and stderr_tail:
            log.debug("CLI stderr tail:\n%s", "\n".join(stderr_tail))


async def drive_turn_events(prompt, mcp_tools, model, stats, effort=None, system_prompt=None,
                            append_system=None):
    """Öffentliche Schnittstelle: der Wire-Strom. Pool (Reuse) oder One-Shot je nach Config."""
    if settings.pool_enabled:
        from .pool import pooled_drive_turn  # lazy: vermeidet Zirkularimport
        async for ev in pooled_drive_turn(prompt, mcp_tools, model, stats, effort,
                                          system_prompt, append_system):
            yield ev
    else:
        async for ev in _oneshot_turn(prompt, mcp_tools, model, stats, effort,
                                      system_prompt, append_system):
            yield ev


async def drive_turn(prompt, mcp_tools, model, stats, effort=None, system_prompt=None,
                     append_system=None):
    """ÜBERGANG: derselbe Turn als alte Tupel, für die OpenAI-Oberflächen in main.py.

    Entfällt, sobald die beiden Endpunkte den Wire-Strom direkt konsumieren. Bis dahin
    bleibt ihr Verhalten unverändert — inklusive der Bündelung mehrerer Tool-Calls einer
    Zeile zu EINEM ('tool_use', blocks)-Tupel, was der alte Vertrag war.
    """
    pending = []
    async for event in drive_turn_events(prompt, mcp_tools, model, stats, effort,
                                         system_prompt, append_system):
        if isinstance(event, wire.ToolCall):
            pending.append(event._raw)
            continue
        if pending:
            yield ("tool_use", pending)
            pending = []
            # Native Wire has a real terminal Done after a tool call. The legacy tuple API
            # represented that terminal solely by tool_use and must not gain an empty result.
            if isinstance(event, wire.Done) and event.stop_reason == "tool_use":
                continue
        if isinstance(event, wire.TextDelta):
            yield ("delta", event.text)
        elif isinstance(event, wire.ThinkingProgress):
            yield ("thinking", event.tokens)
        elif isinstance(event, wire.Done):
            yield ("result", event.text)
        elif isinstance(event, wire.Failed):
            yield ("error", {"type": event.error_type, "message": event.message,
                             "status": event.upstream_status})
    if pending:
        yield ("tool_use", pending)
