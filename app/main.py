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
from .config import settings
from .cli_driver import drive_turn
from .metrics import metrics
from .translate import (
    EFFORT_LEVELS,
    ThinkingProgress,
    finish_from_stop,
    map_effort,
    map_model,
    messages_to_prompt,
    openai_tools_to_mcp,
    split_model_effort,
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


app = FastAPI(title="claude-wrapper-advanced", version="1.3.1", lifespan=lifespan)

_ZERO_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


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


@app.get("/v1/models")
async def list_models(req: Request):
    _require_auth(req)
    now = int(time.time())
    ids = []
    for m in settings.models:
        ids.append(m)
        if settings.effort_variants:                 # 'opus:max' etc. -> Picker = Effort-Selektor
            ids += [f"{m}:{lvl}" for lvl in EFFORT_LEVELS]
    return {"object": "list",
            "data": [{"id": i, "object": "model", "created": now, "owned_by": "anthropic"}
                     for i in ids]}


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

    body = await req.json()

    messages = body.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {
            "message": "'messages' must be a non-empty array", "type": "invalid_request_error"}})

    tools = body.get("tools") or []
    req_model = body.get("model") or settings.default_model
    # Modell-Suffix 'opus:max' -> Basismodell + Effort. Der Suffix ist der explizite
    # UI-Pick (Model-Picker als Effort-Selektor) und schlägt den Body-reasoning_effort.
    base_model, effort_from_name = split_model_effort(req_model)
    cli_model = map_model(base_model)
    stream = bool(body.get("stream"))
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
    effort = effort_from_name or map_effort(body)  # Name-Suffix > Body > Env-Default

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
                async for kind, data in drive_turn(prompt, mcp_tools, cli_model, stats, effort):
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
        async for kind, data in drive_turn(prompt, mcp_tools, cli_model, stats, effort):
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
        code = 504 if err.get("type") == "timeout" else 502
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

    body = await req.json()
    try:
        messages = rsp.input_to_messages(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error": {
            "message": str(e), "type": "invalid_request_error"}}) from None
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {
            "message": "'input' must not be empty", "type": "invalid_request_error"}})

    req_model = body.get("model") or settings.default_model
    base_model, effort_from_name = split_model_effort(req_model)
    cli_model = map_model(base_model)
    effort = effort_from_name or map_effort(body)
    stream = bool(body.get("stream"))

    prompt = messages_to_prompt(messages)
    mcp_tools = openai_tools_to_mcp(rsp.tools_to_openai(body.get("tools")))
    stats = {}
    rid = rsp.new_id("resp")
    echo = {"tools": body.get("tools") or [], "tool_choice": body.get("tool_choice", "auto")}

    if stream:
        return StreamingResponse(_responses_stream(rid, req_model, prompt, mcp_tools, cli_model,
                                                   stats, effort, echo),
                                 media_type="text/event-stream")

    metrics.start()
    t0 = time.perf_counter()
    text_parts, tool_calls, result_text, err = [], None, None, None
    try:
        async for kind, data in drive_turn(prompt, mcp_tools, cli_model, stats, effort):
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
        code = 504 if err.get("type") == "timeout" else 502
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


async def _responses_stream(rid, req_model, prompt, mcp_tools, cli_model, stats, effort, echo):
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

        idx = 0                                   # nächster freier output_index
        think = rsp.ThinkingSummary(idx)
        msg_id, msg_open, text_parts = rsp.new_id("msg"), False, []
        output, done = [], False

        async for kind, data in drive_turn(prompt, mcp_tools, cli_model, stats, effort):
            if done:
                continue
            if kind == "thinking":
                if settings.stream_thinking:
                    for name, payload in think.update(data, time.perf_counter()):
                        yield ev(name, payload)
            elif kind == "delta":
                if not data:
                    continue
                if not msg_open:                   # Denkphase abschließen, Text-Item eröffnen
                    for name, payload in think.close():
                        output.append(payload["item"])   # MUSS in output: completed ersetzt die Liste
                        yield ev(name, payload)
                    idx = 1 if think.opened else 0
                    msg_open = True
                    yield ev("response.output_item.added", {
                        "output_index": idx,
                        "item": {"type": "message", "id": msg_id, "role": "assistant",
                                 "status": "in_progress", "content": []}})
                    yield ev("response.content_part.added", {
                        "output_index": idx, "item_id": msg_id, "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []}})
                text_parts.append(data)
                yield ev("response.output_text.delta", {
                    "output_index": idx, "item_id": msg_id, "content_index": 0, "delta": data})
            elif kind == "tool_use":
                for name, payload in think.close():
                    output.append(payload["item"])
                    yield ev(name, payload)
                base = 1 if think.opened else 0
                for i, tc in enumerate(tooluse_to_toolcalls(data)):
                    item = rsp.function_call_item(tc)
                    at = base + i
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
                if not msg_open:                   # Antwort kam nur im result-Event
                    for name, payload in think.close():
                        output.append(payload["item"])
                        yield ev(name, payload)
                    idx = 1 if think.opened else 0
                    yield ev("response.output_item.added", {
                        "output_index": idx,
                        "item": {"type": "message", "id": msg_id, "role": "assistant",
                                 "status": "in_progress", "content": []}})
                    yield ev("response.content_part.added", {
                        "output_index": idx, "item_id": msg_id, "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []}})
                    if text:
                        yield ev("response.output_text.delta", {
                            "output_index": idx, "item_id": msg_id, "content_index": 0,
                            "delta": text})
                item = rsp.message_item(text, msg_id)
                yield ev("response.output_text.done", {
                    "output_index": idx, "item_id": msg_id, "content_index": 0, "text": text})
                yield ev("response.output_item.done", {"output_index": idx, "item": item})
                output.append(item)
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
