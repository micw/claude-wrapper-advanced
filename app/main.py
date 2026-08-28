"""OpenAI-kompatibler FastAPI-Endpoint auf Basis der Claude Code CLI."""
import asyncio
import hmac
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import responses as rsp
from .auth import auth_status
from .config import DEFAULT_EFFORT, settings
from .cli_driver import drive_turn
from .metrics import metrics
from .translate import (
    ApiError,
    ThinkingProgress,
    build_system_prompt,
    finish_from_stop,
    extract_client_system,
    messages_to_prompt,
    openai_tools_to_mcp,
    resolve_request,
    tooluse_to_toolcalls,
)

log = logging.getLogger("proxy")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auth-Status beim Start prüfen und deutlich loggen — der Container läuft auch OHNE
    # Login weiter, damit man reingehen (`claude /login`) oder CLAUDE_CODE_OAUTH_TOKEN setzen kann.
    st = await auth_status(force=True)
    if st.get("loggedIn"):
        log.info("Claude CLI authenticated (%s, plan=%s, %s)",
                 st.get("email"), st.get("subscriptionType"), st.get("authMethod"))
    else:
        log.warning(
            "Claude CLI NOT authenticated (%s). Server is up but requests will 503 until you log in. "
            "Run inside the container:  claude /login   (or set CLAUDE_CODE_OAUTH_TOKEN and restart).",
            st.get("error") or "not logged in")
    if settings.pool_enabled:
        from .pool import pool
        await pool.start_reaper()
    yield
    if settings.pool_enabled:
        from .pool import pool
        await pool.shutdown()


# Die Version wird ab 1.6.0 mit dem Git-Tag mitgeführt. Davor stand hier 1.3.2, während
# die Tags schon bei 1.5.2 lagen — die Tags waren und bleiben die Wahrheit, dieser String
# folgt ihnen von hier an.
app = FastAPI(title="claude-wrapper-advanced", version="1.6.1", lifespan=lifespan)

# Eigenes Vokabular unter /wire/v1 — was in den OpenAI-Formaten keinen Platz hat.
from .wire_api import router as wire_router   # noqa: E402  (nach app-Definition, kein Zyklus)

app.include_router(wire_router)


@app.exception_handler(ApiError)
async def _api_error_handler(req: Request, exc: ApiError):
    """Unbekanntes Modell -> 404, ungültiger Effort -> 400, im OpenAI-Error-Envelope."""
    return JSONResponse(status_code=exc.status, content=exc.envelope())


def _upstream_code(err):
    """CLI-Fehler -> HTTP-Status. Die CLI liefert bei Modellfehlern 'api_error_status': 404
    mit — den reichen wir durch, statt alles pauschal als 502 auszugeben."""
    if err.get("type") == "timeout":
        return 504
    status = err.get("status")
    return status if isinstance(status, int) and 400 <= status < 600 else 502


_ZERO_USAGE ={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _usage_out(stats):
    """usage-Objekt für die Response; hängt (OpenRouter-Stil) 'cost' an, wenn bekannt."""
    usage = dict(stats.get("usage") or _ZERO_USAGE)
    cost = stats.get("cost_usd")
    if cost is not None:
        usage["cost"] = cost
    think = stats.get("thinking_tokens")
    if think:                                  # OpenAI-Standardort im Chat-Format; gedeckelt wie
        usage["completion_tokens_details"] = {  # in responses.usage_obj (es ist eine Schätzung).
            "reasoning_tokens": min(think, usage.get("completion_tokens", 0))}
    return usage


def _require_auth(req: Request):
    if settings.api_key:
        got = req.headers.get("authorization", "")
        if not hmac.compare_digest(got, f"Bearer {settings.api_key}"):
            raise HTTPException(
                status_code=401,
                detail={"error": {"message": "Invalid API key",
                                  "type": "invalid_request_error", "code": "invalid_api_key"}},
            )


async def _request_json(req: Request):
    """Read JSON with an enforced limit, including chunked requests without Content-Length."""
    limit = settings.max_request_body_bytes
    content_length = req.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > limit:
                raise HTTPException(status_code=413, detail={"error": {
                    "message": f"Request body exceeds the {limit}-byte limit",
                    "type": "invalid_request_error", "code": "request_too_large"}})
        except ValueError:
            pass

    chunks = []
    size = 0
    async for chunk in req.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail={"error": {
                "message": f"Request body exceeds the {limit}-byte limit",
                "type": "invalid_request_error", "code": "request_too_large"}})
        chunks.append(chunk)
    return json.loads(b"".join(chunks))


def _log_req(model, stream, stats, total_ms):
    usage = stats.get("usage") or {}
    ptd = usage.get("prompt_tokens_details") or {}
    log.info(
        "req model=%s stream=%s outcome=%s reused=%s total=%.0fms ttft=%s spawn=%.0fms cli_dur=%s "
        "tokens=%s cached=%s/%s cost=%s",
        model, stream, stats.get("outcome"), stats.get("reused"), total_ms,
        f"{stats['ttft_ms']:.0f}ms" if stats.get("ttft_ms") is not None else "-",
        stats.get("spawn_ms") or 0, stats.get("cli_duration_ms"),
        usage.get("total_tokens"), ptd.get("cached_tokens"), usage.get("prompt_tokens"),
        stats.get("cost_usd"),
    )


def _record(model, stream, stats, total_ms):
    metrics.end(stats.get("outcome") or "error", total_ms=total_ms,
                ttft_ms=stats.get("ttft_ms"), spawn_ms=stats.get("spawn_ms"),
                cli_dur_ms=stats.get("cli_duration_ms"), usage=stats.get("usage"))
    _log_req(model, stream, stats, total_ms)


@app.get("/healthz")
async def healthz():
    # 200 auch ohne Login (Container soll erreichbar bleiben, damit man sich einloggen kann);
    # der Auth-Zustand steht im Body.
    st = await auth_status()
    return {"status": "ok", "model_default": settings.default_model,
            "authenticated": bool(st.get("loggedIn")),
            "auth": {"email": st.get("email"), "plan": st.get("subscriptionType"),
                     "method": st.get("authMethod")} if st.get("loggedIn")
                    else {"error": st.get("error") or "not logged in"}}


@app.get("/metrics")
async def get_metrics():
    snap = metrics.snapshot()
    if settings.pool_enabled:
        from .pool import pool
        snap["pool"] = pool.snapshot()
    return snap


def _model_obj(mid, name, ctx, levels, now, pinned=None):
    """Ein /v1/models-Eintrag. Die Zusatzfelder sind OpenRouter-Konvention; strikte
    OpenAI-Clients ignorieren sie, open-webui nutzt 'name' als Anzeigenamen."""
    params = ["tools", "tool_choice"]
    obj = {"id": mid, "object": "model", "created": now, "owned_by": "anthropic",
           "name": name, "context_length": ctx}
    if pinned:                       # Variante: die Effort-Wahl IST die ID
        obj["reasoning"] = {"default_enabled": True, "default_effort": pinned}
    elif levels:
        params = ["reasoning", "reasoning_effort"] + params
        obj["reasoning"] = {"mandatory": False, "default_enabled": True,
                            "supported_efforts": list(reversed(levels)),
                            "default_effort": DEFAULT_EFFORT}
    obj["supported_parameters"] = params
    return obj


@app.get("/v1/models")
async def list_models(req: Request):
    _require_auth(req)
    now = int(time.time())
    data = []
    for mid, (_cli, name, ctx, levels, _cutoff) in settings.models.items():
        data.append(_model_obj(mid, name, ctx, levels, now))
    for alias, target in settings.aliases.items():
        _cli, name, ctx, levels, _cutoff = settings.models[target]
        data.append(_model_obj(alias, f"{name.split()[0]} (latest)", ctx, levels, now))
    for alias, eff in settings.effort_picks:
        target = settings.aliases.get(alias, alias)
        entry = settings.models.get(target)
        if entry is None or eff not in entry[3]:     # Fehlkonfiguration nicht ausliefern
            log.warning("EFFORT_PICKS: '%s:%s' übersprungen (Modell oder Stufe unbekannt)",
                        alias, eff)
            continue
        _cli, name, ctx, _levels, _cutoff = entry
        data.append(_model_obj(f"{alias}:{eff}", f"{name.split()[0]} · {eff} effort",
                               ctx, (), now, pinned=eff))
    return {"object": "list", "data": data}


def _chunk(cid, model, delta, finish=None):
    return {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _sse(obj) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    _require_auth(req)

    # CLI-Login prüfen (gecacht) — sonst klare 503 statt kryptischer CLI-Abbrüche.
    st = await auth_status()
    if not st.get("loggedIn"):
        raise HTTPException(status_code=503, detail={"error": {
            "message": ("Claude CLI is not authenticated. Log in inside the container "
                        "(`claude /login`, e.g. `docker compose exec proxy claude /login`) "
                        "or set CLAUDE_CODE_OAUTH_TOKEN and restart."),
            "type": "server_error", "code": "not_authenticated"}})

    body = await _request_json(req)

    messages = body.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {
            "message": "'messages' must be a non-empty array", "type": "invalid_request_error"}})

    tools = body.get("tools") or []
    # Modell-Suffix 'opus:max' -> Basismodell + Effort. Der Suffix ist der explizite
    # UI-Pick (Model-Picker als Effort-Selektor) und schlägt den Body-reasoning_effort.
    # Unbekanntes Modell/Effort wirft ApiError (404/400) statt still auf Default zu fallen.
    cli_model, req_model, effort, identity, cutoff = resolve_request(body.get("model"), body)
    stream = bool(body.get("stream"))
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

    append_system, messages = extract_client_system(messages)
    system_prompt = build_system_prompt(identity, cutoff)   # None, wenn REPLACE_SYSTEM_PROMPT=0
    prompt = messages_to_prompt(messages)
    mcp_tools = openai_tools_to_mcp(tools)
    stats = {}

    # ---------- Streaming (SSE) ----------
    if stream:
        cid = "chatcmpl-" + uuid.uuid4().hex

        async def gen():
            metrics.start()
            t0 = time.perf_counter()
            try:
                yield _sse(_chunk(cid, req_model, {"role": "assistant", "content": ""}))
                done = False
                think = ThinkingProgress()
                # Generator VOLL konsumieren (nicht break), damit der Pool die Instanz
                # sauber draint/zurückgibt; nach dem Terminal-Event kommt nichts mehr.
                async for kind, data in drive_turn(prompt, mcp_tools, cli_model, stats, effort, system_prompt, append_system):
                    if done:
                        continue
                    if kind == "delta":
                        if data:
                            yield _sse(_chunk(cid, req_model, {"content": data}))
                    elif kind == "thinking":
                        # Fortschritt statt Denktext (die CLI redigiert ihn). Clients lesen
                        # reasoning_content (OpenWebUI: reasoning_content > reasoning > thinking).
                        if settings.stream_thinking:
                            line = think.update(data, time.perf_counter())
                            if line:
                                yield _sse(_chunk(cid, req_model, {"reasoning_content": line}))
                    elif kind == "tool_use":
                        tcs = tooluse_to_toolcalls(data)
                        yield _sse(_chunk(cid, req_model,
                                          {"tool_calls": [{"index": i, **tc} for i, tc in enumerate(tcs)]}))
                        yield _sse(_chunk(cid, req_model, {}, finish="tool_calls"))
                        done = True
                    elif kind == "result":
                        yield _sse(_chunk(cid, req_model, {},
                                          finish=finish_from_stop(stats.get("stop_reason"))))
                        done = True
                    elif kind == "error":
                        log.error("stream error: %s", data)
                        yield _sse({"error": {"message": data.get("message"),
                                              "type": data.get("type", "api_error")}})
                        done = True
                if include_usage:
                    yield _sse({"id": cid, "object": "chat.completion.chunk",
                                "created": int(time.time()), "model": req_model,
                                "choices": [], "usage": _usage_out(stats)})
                yield "data: [DONE]\n\n"
            except (asyncio.CancelledError, GeneratorExit):
                stats["outcome"] = "cancelled"    # weggeklickter Chat ist kein Serverfehler
                raise
            finally:
                _record(req_model, True, stats, (time.perf_counter() - t0) * 1000)

        return StreamingResponse(gen(), media_type="text/event-stream")

    # ---------- Non-Streaming ----------
    metrics.start()
    t0 = time.perf_counter()
    text_parts, tool_calls, result_text, err = [], None, None, None
    # _record MUSS ins finally: bricht der Client ab (OpenWebUI-Hintergrundtasks tun das
    # regelmäßig), fliegt ein CancelledError daran vorbei und inflight bliebe für immer erhöht.
    try:
        # Generator VOLL konsumieren (nicht break) -> Pool kann die Instanz sauber draina/zurückgeben.
        async for kind, data in drive_turn(prompt, mcp_tools, cli_model, stats, effort, system_prompt, append_system):
            if kind == "delta":
                text_parts.append(data)
            elif kind == "tool_use":
                tool_calls = tooluse_to_toolcalls(data)
            elif kind == "result":
                result_text = data
            elif kind == "error":
                err = data
    except FileNotFoundError:
        stats["outcome"] = "error"
        raise HTTPException(status_code=502, detail={"error": {
            "message": f"Claude CLI '{settings.claude_bin}' nicht gefunden", "type": "api_error"}})
    except asyncio.CancelledError:
        stats["outcome"] = "cancelled"
        raise
    finally:
        _record(req_model, False, stats, (time.perf_counter() - t0) * 1000)

    if err is not None:
        code = _upstream_code(err)
        return JSONResponse(status_code=code,
                            content={"error": {"message": err.get("message"),
                                               "type": err.get("type", "api_error")}})

    cid = "chatcmpl-" + uuid.uuid4().hex
    if tool_calls:
        message = {"role": "assistant", "content": None, "tool_calls": tool_calls}
        finish = "tool_calls"
    else:
        text = result_text if result_text is not None else "".join(text_parts)
        message = {"role": "assistant", "content": text}
        finish = finish_from_stop(stats.get("stop_reason"))

    return JSONResponse({
        "id": cid, "object": "chat.completion", "created": int(time.time()),
        "model": req_model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": _usage_out(stats),
    })


# ---------------------------------------------------------------- /v1/responses
@app.post("/v1/responses")
async def responses(req: Request):
    """OpenAI Responses API — zweiter Endpunkt auf derselben Pipeline.

    Zustandslos: kein store/previous_response_id. Der Request wird auf OpenAI-messages gemappt
    und läuft danach durch exakt denselben Prompt-Bau wie /v1/chat/completions.
    """
    _require_auth(req)

    st = await auth_status()
    if not st.get("loggedIn"):
        raise HTTPException(status_code=503, detail={"error": {
            "message": "Claude CLI is not authenticated.",
            "type": "server_error", "code": "not_authenticated"}})

    body = await _request_json(req)
    try:
        messages = rsp.input_to_messages(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error": {
            "message": str(e), "type": "invalid_request_error"}}) from None
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {
            "message": "'input' must not be empty", "type": "invalid_request_error"}})

    cli_model, req_model, effort, identity, cutoff = resolve_request(body.get("model"), body)
    stream = bool(body.get("stream"))

    append_system, messages = extract_client_system(messages)
    system_prompt = build_system_prompt(identity, cutoff)   # None, wenn REPLACE_SYSTEM_PROMPT=0
    prompt = messages_to_prompt(messages)
    mcp_tools = openai_tools_to_mcp(rsp.tools_to_openai(body.get("tools")))
    stats = {}
    rid = rsp.new_id("resp")
    echo = {"tools": body.get("tools") or [], "tool_choice": body.get("tool_choice", "auto")}

    if stream:
        return StreamingResponse(_responses_stream(rid, req_model, prompt, mcp_tools, cli_model,
                                                   stats, effort, echo, system_prompt, append_system),
                                 media_type="text/event-stream")

    metrics.start()
    t0 = time.perf_counter()
    text_parts, tool_calls, result_text, err = [], None, None, None
    try:
        async for kind, data in drive_turn(prompt, mcp_tools, cli_model, stats, effort, system_prompt, append_system):
            if kind == "delta":
                text_parts.append(data)
            elif kind == "tool_use":
                tool_calls = tooluse_to_toolcalls(data)
            elif kind == "result":
                result_text = data
            elif kind == "error":
                err = data
    except asyncio.CancelledError:
        stats["outcome"] = "cancelled"
        raise
    finally:
        _record(req_model, False, stats, (time.perf_counter() - t0) * 1000)

    if err is not None:
        code = _upstream_code(err)
        return JSONResponse(status_code=code, content=rsp.envelope(
            rid, req_model, [], status="failed", error={
                "code": err.get("type", "api_error"), "message": err.get("message")},
            **_rsp_ctx(stats, echo)))

    output = ([rsp.function_call_item(tc) for tc in tool_calls] if tool_calls
              else [rsp.message_item(result_text if result_text is not None else "".join(text_parts))])
    status, incomplete = rsp.status_from_stop(stats.get("stop_reason"))
    return JSONResponse(rsp.envelope(rid, req_model, output, status=status,
                                     incomplete_details=incomplete, **_rsp_ctx(stats, echo)))


_STATELESS = {"error": {
    "message": ("Stored responses are not supported: this endpoint keeps no state, so there is "
                "nothing to retrieve, cancel or delete. Every request must carry its full `input`."),
    "type": "not_implemented"}}


@app.get("/v1/responses/{response_id}")
@app.delete("/v1/responses/{response_id}")
async def responses_stateless_route(response_id: str):
    """Ohne 501 käme hier ein 404 — das liest sich wie 'ID unbekannt' statt 'gibt es hier nicht'."""
    return JSONResponse(status_code=501, content=_STATELESS)


@app.post("/v1/responses/{response_id}/cancel")
async def responses_cancel(response_id: str):
    return JSONResponse(status_code=501, content=_STATELESS)


def _rsp_ctx(stats, echo):
    """Envelope-Felder, die aus Request-Kontext und Turn-Statistik kommen."""
    return {"usage": stats.get("usage"), "thinking_tokens": stats.get("thinking_tokens", 0),
            "cost": stats.get("cost_usd"), **echo}


async def _responses_stream(rid, req_model, prompt, mcp_tools, cli_model, stats, effort, echo,
                            system_prompt=None, append_system=None):
    """SSE im Responses-Event-Format. Reihenfolge: created -> in_progress -> Items -> completed."""
    metrics.start()
    t0 = time.perf_counter()
    seq = [0]

    def ev(name, payload):
        seq[0] += 1
        return "event: " + name + "\n" + _sse({"type": name, "sequence_number": seq[0], **payload})

    try:
        empty = rsp.envelope(rid, req_model, [], status="in_progress", **echo)
        yield ev("response.created", {"response": empty})
        yield ev("response.in_progress", {"response": empty})

        # Index IMMER aus len(output) ableiten statt zu rechnen: Denkphase, Text und Tool-Calls
        # können in EINEM Turn gemischt auftreten (Claude kündigt den Tool-Aufruf gern an), und
        # eine Kollision würde ein Item überschreiben.
        think = rsp.ThinkingSummary(0)
        msg_id, msg_idx, text_parts = rsp.new_id("msg"), None, []
        output, done = [], False

        async for kind, data in drive_turn(prompt, mcp_tools, cli_model, stats, effort, system_prompt, append_system):
            if done:
                continue
            if kind == "thinking":
                if settings.stream_thinking:
                    for name, payload in think.update(data, time.perf_counter()):
                        yield ev(name, payload)
                continue

            # Jedes andere Event beendet die Denkphase — close() ist idempotent, sonst käme das
            # reasoning-Item bei Text UND Tool-Call im selben Turn doppelt in die Liste.
            for name, payload in think.close():
                output.append(payload["item"])       # MUSS in output: completed ersetzt die Liste
                yield ev(name, payload)

            if kind == "delta":
                if not data:
                    continue
                if msg_idx is None:
                    msg_idx = len(output)
                    for name, payload in rsp.message_open_events(msg_id, msg_idx):
                        yield ev(name, payload)
                text_parts.append(data)
                yield ev("response.output_text.delta", {
                    "output_index": msg_idx, "item_id": msg_id, "content_index": 0, "delta": data})
            elif kind == "tool_use":
                if msg_idx is not None:              # angefangenen Text ZUERST abschließen, sonst
                    text = "".join(text_parts)       # fehlt er im Envelope und der Client wirft
                    for name, payload in rsp.message_close_events(msg_id, msg_idx, text):
                        yield ev(name, payload)      # das bereits Gestreamte wieder weg
                    output.append(rsp.message_item(text, msg_id))
                    msg_idx = None
                for tc in tooluse_to_toolcalls(data):
                    item = rsp.function_call_item(tc)
                    at = len(output)
                    yield ev("response.output_item.added", {
                        "output_index": at,
                        "item": {**item, "arguments": "", "status": "in_progress"}})
                    yield ev("response.function_call_arguments.delta", {
                        "output_index": at, "item_id": item["id"], "delta": item["arguments"]})
                    yield ev("response.function_call_arguments.done", {
                        "output_index": at, "item_id": item["id"], "arguments": item["arguments"]})
                    yield ev("response.output_item.done", {"output_index": at, "item": item})
                    output.append(item)
                done = True
            elif kind == "result":
                text = data if data else "".join(text_parts)
                if msg_idx is None:                  # Antwort kam nur im result-Event
                    msg_idx = len(output)
                    for name, payload in rsp.message_open_events(msg_id, msg_idx):
                        yield ev(name, payload)
                    if text:
                        yield ev("response.output_text.delta", {
                            "output_index": msg_idx, "item_id": msg_id, "content_index": 0,
                            "delta": text})
                for name, payload in rsp.message_close_events(msg_id, msg_idx, text):
                    yield ev(name, payload)
                output.append(rsp.message_item(text, msg_id))
                done = True
            elif kind == "error":
                log.error("responses stream error: %s", data)
                yield ev("response.failed", {"response": rsp.envelope(
                    rid, req_model, output, status="failed",
                    error={"code": data.get("type", "api_error"),
                           "message": data.get("message")}, **_rsp_ctx(stats, echo))})
                return

        # IMMER response.completed als Terminal-Event, auch bei status "incomplete". Die Spec
        # sähe response.incomplete vor, aber Clients werten es nicht aus: gegen OpenWebUIs echten
        # Handler geprüft, response.incomplete liefert meta=None -> usage UND das done-Signal
        # gehen verloren, die Antwort bliebe als unfertig hängen. Der Status steht im Envelope,
        # ein spec-kundiger Client findet ihn dort.
        status, incomplete = rsp.status_from_stop(stats.get("stop_reason"))
        yield ev("response.completed", {"response": rsp.envelope(
            rid, req_model, output, status=status, incomplete_details=incomplete,
            **_rsp_ctx(stats, echo))})
    except (asyncio.CancelledError, GeneratorExit):
        stats["outcome"] = "cancelled"
        raise
    finally:
        _record(req_model, True, stats, (time.perf_counter() - t0) * 1000)
