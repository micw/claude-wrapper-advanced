"""Eigenes Vokabular unter `/wire/v1` — alles, wofür die OpenAI-Formate kein Feld haben.

Getrennt von `/v1`, weil es eine eigene Entscheidung über die Exponierung ist: ein
Reverse-Proxy kommt mit einer Regel pro Präfix aus. Versioniert von Anfang an, weil sich
`wire.Event` noch bewegt.

Zwei Endpunkte:

* `POST /responses` — ein Turn als SSE aus `wire`-Ereignissen. Was die OpenAI-Oberflächen
  verwerfen müssen (Kontingent-Alarm, Kosten pro Modell, Cache-Aufteilung nach TTL,
  Prozess-Zeiten), steht hier drin.
* `GET /usage` — der Kontingent-Stand des Kontos, die einzige Quelle für Füllstände.

Die beiden hängen zusammen: ein `limit_status` im Strom nennt ein Fenster, aber keine
Zahl — und schickt den Konsumenten damit an `/usage`.
"""
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import limits
from .auth import auth_status, require_api_key
from .cli_driver import drive_turn_events
from .config import settings
from .metrics import metrics
from .translate import (ApiError, build_system_prompt, extract_client_system,
                        messages_to_prompt, openai_tools_to_mcp, resolve_request)

log = logging.getLogger("wire")

router = APIRouter(prefix="/wire/v1", tags=["wire"])


async def _body(req: Request):
    """JSON mit Größengrenze lesen.

    Dieselbe Grenze wie in main._request_json; die beiden gehören zusammengelegt, sobald
    der Arbeitsbaum dafür frei ist (der Import wäre heute zirkulär).
    """
    limit = getattr(settings, "max_request_body_bytes", 32 << 20)
    size, chunks = 0, []
    async for chunk in req.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail={"error": {
                "message": f"Request body exceeds the {limit}-byte limit",
                "type": "invalid_request_error", "code": "request_too_large"}})
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks))
    except ValueError as err:
        raise HTTPException(status_code=400, detail={"error": {
            "message": f"invalid JSON: {err}", "type": "invalid_request_error"}}) from None


@router.post("/responses")
async def responses(req: Request):
    """Ein Turn als SSE. Jedes Ereignis ist ein JSON-Objekt in `data:`, der Typ steht im
    Feld `type` — kein `event:`-Feld, damit der Konsument nur an einer Stelle nachsieht.

    Nimmt dieselben `messages` und `tools` wie `/v1/chat/completions` entgegen: die
    Übersetzung nach innen ist identisch, unterschiedlich ist nur, was **heraus**kommt.
    Bricht der Client die Verbindung ab, endet der Generator und der Turn mit ihm.
    """
    require_api_key(req)
    status = await auth_status()
    if not status.get("loggedIn"):
        raise HTTPException(status_code=503, detail={"error": {
            "message": "Claude CLI is not authenticated.",
            "type": "server_error", "code": "not_authenticated"}})

    body = await _body(req)
    messages = body.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {
            "message": "'messages' must be a non-empty array",
            "type": "invalid_request_error"}})

    try:
        cli_model, _req_model, effort, identity, cutoff = resolve_request(body.get("model"), body)
    except ApiError as err:
        raise HTTPException(status_code=err.status, detail=err.envelope()) from None

    append_system, messages = extract_client_system(messages)
    system_prompt = build_system_prompt(identity, cutoff)
    prompt = messages_to_prompt(messages)
    mcp_tools = openai_tools_to_mcp(body.get("tools") or [])
    stats = {}

    async def stream():
        metrics.start()
        t0 = time.perf_counter()
        try:
            async for event in drive_turn_events(prompt, mcp_tools, cli_model, stats,
                                                 effort, system_prompt, append_system):
                yield "data: " + json.dumps(event.payload(), ensure_ascii=False) + "\n\n"
        except BaseException:
            # Weggeklickter Client ist kein Serverfehler — sonst zählt die Fehlerrate ihn mit.
            stats.setdefault("outcome", "cancelled")
            raise
        finally:
            total_ms = (time.perf_counter() - t0) * 1000
            metrics.end(stats.get("outcome") or "error", total_ms=total_ms,
                        ttft_ms=stats.get("ttft_ms"), spawn_ms=stats.get("spawn_ms"),
                        cli_dur_ms=stats.get("cli_duration_ms"), usage=stats.get("usage"))
            log.info("wire model=%s outcome=%s reused=%s total=%.0fms",
                     cli_model, stats.get("outcome"), stats.get("reused"), total_ms)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/usage")
async def get_usage(req: Request, force: bool = False):
    """Kontingent des Kontos: welche Fenster es gibt, wem sie gehören, wie voll sie sind.

    Ein Request an das Backend, gecacht (`USAGE_TTL`, Standard 60 s). `?force=1` umgeht
    den Cache — gedacht für den Fall, dass ein `limit_status` im Turn gemeldet hat und
    der Konsument den frischen Stand braucht.

    Die einzige Quelle für Füllstände und für die Frage, welchem **Modell** ein Fenster
    gehört: das Turn-Ereignis trägt beides nicht (MESSUNGEN.md §4.1).
    """
    require_api_key(req)
    try:
        return await limits.usage(force=force)
    except limits.UsageUnavailable as err:
        # 503 und nicht 502: der Dienst arbeitet, nur diese Auskunft steht gerade nicht
        # zur Verfügung. Turns laufen davon unberührt weiter.
        log.warning("usage unavailable: %s", err)
        return JSONResponse(status_code=503, content={"error": {
            "message": f"quota state unavailable: {err}",
            "type": "server_error", "code": "usage_unavailable"}})
