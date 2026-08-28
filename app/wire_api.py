"""Eigenes Vokabular unter `/wire/v1` — alles, wofür die OpenAI-Formate kein Feld haben.

Getrennt von `/v1`, weil es eine eigene Entscheidung über die Exponierung ist: ein
Reverse-Proxy kommt mit einer Regel pro Präfix aus.

Vorerst nur der Kontingent-Stand. Er hat hier seinen Platz, weil ihn beide OpenAI-
Oberflächen still verwerfen würden — und weil der Turn-Alarm (`limit_status`) den
Konsumenten genau hierher schickt, wenn ein Limit greift.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import limits
from .auth import require_api_key

log = logging.getLogger("wire")

router = APIRouter(prefix="/wire/v1", tags=["wire"])


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
