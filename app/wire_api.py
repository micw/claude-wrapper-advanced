"""Eigenes Vokabular unter `/wire/v1` — alles, wofür die OpenAI-Formate kein Feld haben.

Getrennt von `/v1`, weil es eine eigene Entscheidung über die Exponierung ist: ein
Reverse-Proxy kommt mit einer Regel pro Präfix aus. Versioniert von Anfang an, weil sich
`wire.Event` noch bewegt.

Vier Endpunkte:

* `POST /responses` — ein Turn als SSE aus `wire`-Ereignissen. Was die OpenAI-Oberflächen
  verwerfen müssen (Kontingent-Alarm, Kosten pro Modell, Cache-Aufteilung nach TTL,
  Prozess-Zeiten), steht hier drin.
* `GET /usage` — der Kontingent-Stand des Kontos, die einzige Quelle für Füllstände.
* `GET /models` — die Registry ohne die Pseudo-Einträge, die `/v1/models` für Model-Picker
  erzeugt (Aliase, Effort-Picks).
* `GET /info` — Dienst und Version. Bewusst schmal; die Vertragsversion steht im Pfad.

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
from .config import DEFAULT_EFFORT, SERVICE, VERSION, clamp_effort, settings
from .metrics import metrics
from .translate import (ApiError, build_system_prompt, extract_client_system,
                        messages_to_prompt, openai_tools_to_mcp, resolve_request)

log = logging.getLogger("wire")

router = APIRouter(prefix="/wire/v1", tags=["wire"])


@router.get("/info")
async def info(req: Request):
    """Womit spricht der Konsument — Dienst und Version, mehr nicht.

    Bewusst schmal. Die **Vertragsversion** steht bereits im Pfad (`/wire/v1`), ein eigenes
    Feld dafür wäre Dopplung; und Fähigkeiten gehören dorthin, wo sie gelten: Modelle nach
    `/models`, Kontingent nach `/usage`.

    `version` ist die Release-Version und wandert mit dem Git-Tag. Ein Konsument, der sein
    Verhalten daran festmacht, macht es am Falschen fest — dafür ist die Pfadversion da.
    Sie taugt für Logs, Fehlerberichte und die Frage „läuft das Deployment schon neu?".
    """
    require_api_key(req)
    return {"service": SERVICE, "version": VERSION}


@router.get("/models")
async def models(req: Request):
    """Die Modell-Registry, ungeschminkt.

    Unterschied zu `/v1/models`: dort werden aus sechs echten Modellen vierzehn Einträge,
    weil Aliase und Effort-Picks (`opus:max`) als Pseudo-Modelle mitlaufen — das braucht ein
    Model-Picker, der die Effort-Wahl über die Modellauswahl abbilden muss. Ein Konsument
    dieser API braucht das Gegenteil: jedes Modell **einmal**, mit seinen Stufen als Feld.

    `backend_model` ist der Name, den die CLI kennt — und derselbe Schlüssel, unter dem
    `done.cost.by_model` abrechnet. Nur damit lässt sich eine Kostenzeile einem Modell
    dieser Liste zuordnen.
    """
    require_api_key(req)
    aliases = {}
    for alias, target in settings.aliases.items():
        aliases.setdefault(target, []).append(alias)

    out = []
    for mid, (cli_model, name, ctx, levels, cutoff, input_modalities) in settings.models.items():
        # Der Env-Default gilt nur, soweit das Modell ihn kennt — dieselbe Absenkung, die
        # ein Request erfährt. Ohne Stufen (Haiku) gibt es keinen Default, nicht "high".
        default = clamp_effort(settings.effort or DEFAULT_EFFORT, levels) if levels else None
        out.append({
            "id": mid,
            "name": name,
            "backend_model": cli_model,
            "context_length": ctx,
            "input_modalities": list(input_modalities),
            "efforts": {"supported": list(levels), "default": default},
            "knowledge_cutoff": cutoff,
            "aliases": aliases.get(mid, []),
        })
    return {"models": out}


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
async def get_usage(req: Request):
    """Kontingent des Kontos: welche Fenster es gibt, wem sie gehören, wie voll sie sind.

    Ein Request an das Backend, gecacht (`USAGE_TTL`, Standard 60 s). Häufiger zu fragen
    bringt nichts: die Auflösung des Füllstands ist ein Prozentpunkt, und im Minutentakt
    bewegt sich nichts (MESSUNGEN.md §1).

    Die einzige Quelle für Füllstände und für die Frage, welchem **Modell** ein Fenster
    gehört: das Turn-Ereignis trägt beides nicht (MESSUNGEN.md §4.1).
    """
    require_api_key(req)
    try:
        return await limits.usage()
    except limits.UsageUnavailable as err:
        # 503 und nicht 502: der Dienst arbeitet, nur diese Auskunft steht gerade nicht
        # zur Verfügung. Turns laufen davon unberührt weiter.
        #
        # Mit `Retry-After`, wo wir eine Zeit kennen. Die Gegenstelle nennt sie im 429
        # (beobachtet: 871 s), wir werten sie intern längst aus — sie aber für uns zu
        # behalten heißt, dass der Konsument raten muss. Beobachtet: fünf Versuche in drei
        # Sekunden, was die Drosselung nur verlängert. Der Header ist bei einem 503 der
        # vorgesehene Weg (RFC 9110); das Feld im Body ist für Clients, die nur JSON lesen.
        retry = None if err.retry_after is None else max(0, int(err.retry_after))
        log.warning("usage unavailable: %s (retry_after=%s)", err, retry)
        body = {"message": f"quota state unavailable: {err}",
                "type": "server_error", "code": "usage_unavailable"}
        headers = {}
        if retry is not None:
            body["retry_after"] = retry
            headers["Retry-After"] = str(retry)
        return JSONResponse(status_code=503, content={"error": body}, headers=headers)
