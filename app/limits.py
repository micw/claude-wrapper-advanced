"""Kontingente: eine Form aus zwei Quellen.

Das Backend meldet den Stand zweimal, mit sehr verschiedenen Stärken:

* **`GET /api/oauth/usage`** — ein eigener Request, aber vollständig: alle Fenster mit
  Füllstand, dazu `scope` (welchem Modell ein Fenster gehört) und die Guthaben-Blöcke.
* **das `rate_limit_event` jedes Turns** — kostenlos, kommt vor dem ersten Token, trägt
  aber **keine Füllstände** (gemessen: `utilization` schickt das Backend nur bei
  `allowed_warning` und beim 429). Es taugt als *Alarm*, nicht als Anzeige.

Beide werden hier auf denselben Fensterschlüssel projiziert, damit ein Konsument nicht
wissen muss, aus welcher Quelle ein Objekt stammt. Die Messungen dahinter stehen in
MESSUNGEN.md §4 bis §6.

# Warum der Schlüssel gebaut und nicht übernommen wird

Die beiden Quellen benennen dieselben Fenster verschieden. Kontoweit stimmen sie überein
(`five_hour`, `seven_day` — in beiden wörtlich gleich). Beim modell-skopierten Fenster
nicht: der Turn sagt `seven_day_overage_included`, die Usage-API kennt dafür **keinen**
Schlüssel und beschreibt es stattdessen als `kind: weekly_scoped` mit
`scope.model.display_name: "Fable"`. Ein gemeinsamer Schlüssel existiert in den Daten
nicht, und `resets_at` hilft nicht — das Fable-Fenster setzt zur selben Sekunde zurück wie
`seven_day`.

Deshalb wird der Schlüssel aus **Dauer + Geltungsbereich** gebildet, was beide Quellen
hergeben: `global/seven_day` bzw. `model:fable-5/seven_day`. Auf der Turn-Seite steuert
der Wrapper den Geltungsbereich bei — er weiß, welches Modell er gefahren hat. Das ist
keine Heuristik, sondern der Parameter, den er selbst gesetzt hat.

**Bedingung, unter der das trägt:** ein Modell hat höchstens ein skopiertes Fenster.
Heute erfüllt. Wäre sie verletzt, ließen sich zwei skopierte Fenster desselben Modells aus
der Turn-Meldung nicht unterscheiden — dann liefert `window_key()` `None`, und der
Konsument lädt über die API nach, statt eine Zuordnung zu raten.
"""
import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime

from .config import settings

log = logging.getLogger("limits")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"

# Fensterlängen, im CLI fest verdrahtet und hier gespiegelt. Nur zur Anreicherung —
# fehlt ein Eintrag, bleibt window_seconds None statt zu raten.
WINDOW_SECONDS = {"five_hour": 18000, "seven_day": 604800}

# Fenster, deren limits[]-Eintrag gemessen `scope: null` trägt, die also für jedes Modell
# gelten. Ein Claim, der hier nicht steht, ist skopiert und braucht das Modell des Turns.
ACCOUNT_WIDE = ("five_hour", "seven_day")

# Wie lange eine Usage-Antwort wiederverwendet wird. Die Auflösung des Backends beträgt
# einen Prozentpunkt und im Leerlauf bewegt sich der Zähler nicht (MESSUNGEN.md §1) —
# häufiger zu fragen liefert dieselbe Zahl.
# TODO: gehört nach Settings, sobald der Arbeitsbaum dafür frei ist.
USAGE_TTL = float(os.getenv("USAGE_TTL", "60"))

_cache = {"at": 0.0, "val": None, "retry_at": None, "last_error": None}
_lock = asyncio.Lock()


# Sperrzeit nach einem Fehlschlag, wenn die Gegenstelle keine `Retry-After` nennt. Ohne
# sie würde JEDER Consumer-Request einen neuen Versuch auslösen — bei einem 429 hielte der
# Wrapper das Limit damit selbst am Leben. Gemessen im Betrieb: die Usage-API antwortet
# durchaus mit 429, ohne dass das Kontingent erschöpft wäre.
FAILURE_BACKOFF = float(os.getenv("USAGE_FAILURE_BACKOFF", "60"))

#: Obergrenze für eine von der Gegenstelle genannte Wartezeit — ein absurder Wert soll den
#: Endpunkt nicht für Stunden stilllegen.
MAX_BACKOFF = 900.0


class UsageUnavailable(Exception):
    """Der Stand konnte nicht geholt werden — mit dem Grund im Klartext."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


# ------------------------------------------------------------------ Schlüssel

def model_key(name):
    """Modellbezeichnung -> externe Modell-Id der Registry.

    Muss von beiden Seiten auf denselben Wert kommen: die Usage-API liefert einen
    Anzeigenamen (`"Fable"`, gemessen ohne Version), der Turn das CLI-Modell
    (`"claude-fable-5"`). Was sich nicht auflösen lässt, wird normalisiert
    durchgereicht statt verworfen — ein unbekanntes Modell soll sichtbar bleiben.
    """
    if not name:
        return None
    n = name.strip().lower()
    if n.startswith("claude-"):
        n = n[len("claude-"):]
    for cand in (n, n.replace(" ", "-"), n.split()[0] if n.split() else n):
        if cand in settings.models:
            return cand
        if cand in settings.aliases:
            return settings.aliases[cand]
    return n.replace(" ", "-")


def _duration(claim):
    """Claim-Id -> Fensterdauer als Schlüsselbestandteil.

    `seven_day_overage_included` und `seven_day_sonnet` sind Sieben-Tage-Fenster; der
    Präfix trägt die Dauer, der Rest benennt den Geltungsbereich, den wir separat führen.
    """
    for known in ("seven_day", "five_hour"):
        if claim == known or claim.startswith(known + "_"):
            return known
    return claim


def window_key(claim, model=None):
    """Turn-Claim -> derselbe Schlüssel, den /wire/v1/usage verwendet.

    `None`, wenn die Zuordnung nicht aus Daten folgt (skopierter Claim ohne bekanntes
    Modell). Der Konsument lädt dann nach — das ist billiger als ein falsches Etikett.
    """
    if not claim:
        return None
    if claim in ACCOUNT_WIDE:
        return f"global/{claim}"
    key = model_key(model)
    return f"model:{key}/{_duration(claim)}" if key else None


# ------------------------------------------------------ Projektion: Usage-API

def _epoch(value):
    """RFC-3339 der Usage-API -> Unix-Sekunden. `None` bleibt `None`."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return None


def _window(entry, now):
    resets_at = _epoch(entry.get("resets_at"))
    percent = entry.get("percent")
    return {
        "used_percent": percent,
        "resets_at": resets_at,
        "resets_in_seconds": max(0, resets_at - now) if resets_at else None,
        # Die API kennt kein 'erreicht'-Feld; `severity` haben wir nur als "normal"
        # gesehen und die CLI wertet es nicht aus. Also aus dem Füllstand ableiten —
        # das verlässlichere Signal ist der Turn (status == "rejected").
        "reached": percent is not None and percent >= 100,
    }


def from_usage(payload):
    """Antwort von /api/oauth/usage -> unser Kontingent-Block.

    Gelesen wird `limits[]`, nicht die Top-Level-Schlüssel: nur dort stehen `kind`,
    `group` und `scope`. Die Top-Level-Liste enthält daneben rotierende Codenamen
    (`nimbus_quill`, `cinder_cove`, …), die in `limits[]` bewusst nicht auftauchen.
    """
    now = int(time.time())
    windows = {}
    for entry in payload.get("limits") or []:
        kind = entry.get("kind")
        scope = entry.get("scope") or {}
        model = ((scope.get("model") or {}) or {}).get("display_name")
        surface = scope.get("surface")
        duration = "five_hour" if kind == "session" else "seven_day"

        if model:
            key = f"model:{model_key(model)}/{duration}"
        elif surface:
            key = f"surface:{surface}/{duration}"
        else:
            key = f"global/{duration}"

        window = _window(entry, now)
        window["window_seconds"] = WINDOW_SECONDS.get(duration)
        # scope: null heißt hier "kontoweit" und nicht "unbekannt" — die Usage-API
        # beantwortet das eindeutig. Aus einem Turn wäre dasselbe null mehrdeutig,
        # deshalb trägt das Turn-Ereignis gar kein scope-Feld.
        window["scope"] = {"model": model, "surface": surface} if (model or surface) else None
        window["is_active"] = entry.get("is_active")
        windows[key] = window

    extra = payload.get("extra_usage") or {}
    spend = payload.get("spend") or {}
    return {
        "windows": windows,
        "credits": {
            "enabled": extra.get("is_enabled"),
            "utilization": extra.get("utilization"),
            "disabled_reason": extra.get("disabled_reason"),
            "spend_limit_reached": extra.get("spend_limit_reached"),
            "can_purchase": spend.get("can_purchase_credits"),
        },
        "fetched_at": now,
    }


# -------------------------------------------------------- Projektion: Turn

def status_from_event(info, model=None):
    """rate_limit_event -> `limit_status`, oder `None`, solange nichts anliegt.

    Bewusst **keine Zahlen**: der Turn hat im Normalfall keine (siehe Modul-Docstring).
    Das Ereignis nennt Zustand, betroffenes Fenster und Reset — und ist damit der Anlass
    für den Konsumenten, den Stand über /wire/v1/usage nachzuladen.

    `None` bei `status == "allowed"`: ein Ereignis pro Turn, das nichts zu melden hat,
    wäre Rauschen im Strom. Für die Statistik wird es trotzdem verbucht (metrics).
    """
    if not info:
        return None
    status = info.get("status")
    overage = info.get("overageStatus")
    # `overageStatus` allein löst NICHT aus: gemessen steht es auf einem Konto ohne
    # Guthaben dauerhaft auf "rejected" (`org_level_disabled`). Das ist eine
    # Konfigurationstatsache, kein Ereignis — sonst käme der Alarm bei jedem Turn.
    # Auslöser sind: das Limit selbst warnt/greift, wir verbrauchen gerade Guthaben,
    # oder das Backend nennt einen Fehlercode.
    if (status in (None, "allowed")
            and not info.get("isUsingOverage")
            and not info.get("errorCode")):
        return None
    claim = info.get("rateLimitType")
    return {
        "window": window_key(claim, model),
        "claim": claim,                     # roh, auch wenn sich kein Fenster zuordnen ließ
        "status": status,
        "resets_at": info.get("resetsAt"),
        "surpassed_threshold": info.get("surpassedThreshold"),
        "overage": {
            "status": overage,
            "in_use": info.get("isUsingOverage"),
            "disabled_reason": info.get("overageDisabledReason"),
            "error_code": info.get("errorCode"),
        },
        # Der Füllstand dieses Fensters ist hier nicht bekannt. Das Flag sagt es dem
        # Konsumenten, statt ihn eine fehlende Zahl als 0 lesen zu lassen.
        "usage_stale": True,
    }


# ------------------------------------------------------------------- Abruf

def _token():
    """OAuth-Token der CLI plus die Quelle. Env schlägt Credential-Datei (Container-Setup).

    Die Quelle wird mitgegeben, weil sie im Fehlerfall die entscheidende Auskunft ist: ein
    langlebiger `setup-token` und die Anmeldung aus dem Login-Volume sind verschiedene
    Dinge, und von außen sieht man nur den Statuscode.
    """
    token = os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        return token.strip(), "CLAUDE_CODE_OAUTH_TOKEN"
    base = os.getenv("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    path = os.path.join(base, ".credentials.json")
    try:
        with open(path) as fh:
            data = (json.load(fh).get("claudeAiOauth") or {})
    except (OSError, ValueError) as err:
        raise UsageUnavailable(f"no OAuth token: {err}") from None
    # Ablaufzeitpunkt nur zur Diagnose: erneuert wird hier nichts, das macht die CLI beim
    # nächsten Turn. Ein abgelaufener Token erklärt aber einen Fehler, den man sonst rät.
    expired = ""
    expires_at = data.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at / 1000 < time.time():
        expired = ", ABGELAUFEN"
    return data.get("accessToken"), f"{path}{expired}"


def _fetch_sync():
    token, source = _token()
    if not token:
        raise UsageUnavailable("no OAuth token in env or credential store")
    request = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        # Retry-After mitnehmen, wo die Gegenstelle eine nennt: bei 429 ist das die einzige
        # Auskunft darüber, wann ein neuer Versuch überhaupt Sinn hat.
        retry = None
        try:
            raw = (err.headers or {}).get("Retry-After")
            retry = min(float(raw), MAX_BACKOFF) if raw else None
        except (TypeError, ValueError):
            retry = None
        # Ins LOG, nicht in die Antwort: Token-Quelle und der Wortlaut der Gegenstelle sind
        # Betriebsdiagnose. Von außen ist ein 429 sonst nicht von einem anderen zu
        # unterscheiden — Token abgelaufen, falscher Token-Typ oder wirklich zu viele
        # Anfragen sehen alle gleich aus.
        detail = ""
        try:
            detail = (err.read() or b"")[:300].decode(errors="replace")
        except Exception:  # noqa: BLE001 — Diagnose darf den Fehlerpfad nie ersetzen
            detail = "(kein Body)"
        log.warning("usage fetch: upstream %s (token aus %s, retry_after=%s): %s",
                    err.code, source, retry, detail)
        raise UsageUnavailable(f"upstream {err.code}", retry_after=retry) from None
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise UsageUnavailable(str(err)) from None


def _fresh(now):
    return _cache["val"] is not None and now - _cache["at"] < USAGE_TTL


def _blocked(now):
    """Läuft noch eine Sperre aus einem vorherigen Fehlschlag?"""
    return _cache["retry_at"] is not None and now < _cache["retry_at"]


def _stale(reason):
    """Der letzte bekannte Stand, als solcher gekennzeichnet.

    Ein alter Füllstand ist brauchbar — die Auflösung beträgt ohnehin einen Prozentpunkt
    und im Leerlauf bewegt sich nichts (MESSUNGEN.md §1). Nur wenn es überhaupt nichts
    gibt, ist das ein Fehler.
    """
    if _cache["val"] is None:
        return None
    return {**_cache["val"], "stale": True, "stale_reason": reason}


async def usage(force=False):
    """Kontingent-Block, gecacht. Ein Poll im Minutentakt verliert nichts (MESSUNGEN.md §1).

    Nach einem Fehlschlag wird **gesperrt**, nicht weiterprobiert: sonst löst jeder
    Consumer-Request einen neuen Upstream-Versuch aus und der Wrapper hält ein 429 selbst
    am Leben. Solange ein Stand bekannt ist, wird der weitergereicht — mit `stale: true`,
    damit niemand ihn für frisch hält. `force` umgeht den Cache, aber **nicht** die Sperre.

    Kein `httpx`: ein einzelner GET rechtfertigt keine Dependency in einem Projekt, das
    mit fastapi+uvicorn auskommt. urllib im Thread, damit der Event-Loop frei bleibt.
    """
    now = time.monotonic()
    if not force and _fresh(now):
        return _cache["val"]
    if _blocked(now):
        stale = _stale(_cache["last_error"])
        if stale is not None:
            return stale
        raise UsageUnavailable(_cache["last_error"])
    async with _lock:
        now = time.monotonic()
        if not force and _fresh(now):
            return _cache["val"]
        if _blocked(now):
            stale = _stale(_cache["last_error"])
            if stale is not None:
                return stale
            raise UsageUnavailable(_cache["last_error"])
        try:
            payload = await asyncio.to_thread(_fetch_sync)
        except UsageUnavailable as err:
            wait = err.retry_after if err.retry_after is not None else FAILURE_BACKOFF
            _cache["retry_at"] = time.monotonic() + wait
            _cache["last_error"] = str(err)
            log.warning("usage fetch failed (%s), retrying in %.0fs", err, wait)
            stale = _stale(str(err))
            if stale is not None:
                return stale
            raise
        value = from_usage(payload)
        _cache["val"] = value
        _cache["at"] = time.monotonic()
        _cache["retry_at"] = None
        _cache["last_error"] = None
        return value
