"""Das Vokabular, das der Wrapper nach außen spricht.

**Bewusst nicht an einen Konsumenten gebunden.** Trüge es die Ereignisnamen eines
bestimmten Clients, wäre der Wrapper daran gekettet; die OpenAI-Oberflächen sind
Konsumenten dieses Stroms, nicht umgekehrt.

Die Felder spiegeln, was die CLI **tatsächlich** liefert (gemessen, MESSUNGEN.md), nicht
was ein Konsument sich wünschen könnte. Wo die Decke der CLI verläuft, fehlt hier ein
Feld, statt eines mitzuführen, das immer leer bleibt:

* `ThinkingProgress` hat **keinen Text** — die CLI redigiert ihn und liefert nur
  Tokenzahlen (MESSUNGEN.md §3).
* Es gibt **kein** Reasoning-Replay-Item: anders als bei der Responses-API gibt es hier
  nichts, was in einen Folge-Turn zurückgespielt werden könnte.
* Pro Turn kommt **höchstens ein** `ToolCall` — die CLI bricht den Turn danach ab.

# Verhältnis zum codex-api-wrapper

Gleiche Grammatik, eigenes Vokabular. Übernommen sind die Regeln, die sich dort bewährt
haben: `null` heißt „unbekannt", Einheiten werden einmal am Rand normalisiert, Backend-Ids
werden wörtlich durchgereicht, und Quellen mit verschiedener Auskunftsfähigkeit bekommen
verschiedene Typen statt eines gemeinsamen mit Löchern.

Nicht übernommen sind die Feldnamen dort, wo die Semantik abweicht — und eine weicht
sichtbar ab: **`input` enthält hier die Cache-Treffer NICHT.** Die CLI meldet
`input_tokens` als den nicht gecachten Rest (gemessen: 2 Tokens neben 3219 Cache-Treffern),
während codex' `input_tokens` die Treffer einschließt. Wer beide Wrapper bedient, darf
also nicht dieselbe Summe bilden — deshalb heißt das Feld `input_new` und die Summe steht
ausgerechnet als `input_total` daneben.
"""
import math
import uuid
from dataclasses import dataclass, field


@dataclass
class Event:
    """Basis. `type` ist der Diskriminator, damit ein Konsument an einer Stelle nachsieht."""

    type = "event"

    def payload(self) -> dict:
        """Serialisierbare Form. Felder mit `_` sind intern und gehen nicht nach außen."""
        data = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        return {"type": self.type, **data}


@dataclass
class Started(Event):
    """Der Turn läuft. `reused` sagt, ob eine warme Instanz aus dem Pool kam — die einzige
    Stelle, an der die Prozess-Natur dieses Wrappers nach außen sichtbar ist."""
    type = "started"
    model: str = ""
    reused: bool = False


@dataclass
class TextDelta(Event):
    type = "text_delta"
    text: str = ""


@dataclass
class ThinkingProgress(Event):
    """Fortschritt der Denkphase — **kein Text**, den liefert die CLI redigiert.

    `tokens` ist die geschätzte Zahl dieses Schrittes (`estimated_tokens`). Die echte
    Gesamtzahl steht am Ende in `Done.usage.thinking`; gemessen lagen 450 geschätzt gegen
    490 echt."""
    type = "thinking_progress"
    tokens: int = 0


@dataclass
class ToolCall(Event):
    """Ein Tool-Aufruf für den Client. `arguments` ist ein JSON-String, kein Objekt —
    so liefert die CLI ihn, und erneutes Parsen würde bei kaputtem JSON Information
    verlieren."""
    type = "tool_call"
    id: str = ""
    name: str = ""
    arguments: str = ""
    # Der unveränderte CLI-Block. Übergangshilfe für den Tupel-Adapter in cli_driver,
    # der die OpenAI-Oberflächen noch bedient; geht nicht nach außen (führendes `_`).
    _raw: dict = field(default_factory=dict)


@dataclass
class LimitStatus(Event):
    """Kontingent-Alarm. Kommt **nur**, wenn etwas anliegt (Warnung, Ablehnung, Guthaben
    in Benutzung) — nicht bei jedem Turn.

    Trägt bewusst keine Füllstände: der Turn kennt keine (MESSUNGEN.md §4.1). `window`
    nennt den Schlüssel, unter dem `GET /wire/v1/usage` dasselbe Fenster führt; ist er
    `null`, ließ sich die Zuordnung nicht aus Daten ableiten und der Konsument lädt nach."""
    type = "limit_status"
    window: str | None = None
    claim: str | None = None
    status: str | None = None
    resets_at: int | None = None
    surpassed_threshold: float | None = None
    overage: dict = field(default_factory=dict)
    usage_stale: bool = True


@dataclass
class Quota(Event):
    """Vollständiger letzter Kontingent-Snapshot nach einer Header-Beobachtung.

    Getrennt von `Done.usage`: dort stehen Tokenzahlen dieses Turns, hier die
    Abo-Fenster. Der Consumer ersetzt seinen Snapshot vollständig statt Deltas zu mergen.
    """
    type = "quota"
    usage: dict = field(default_factory=dict)


@dataclass
class Done(Event):
    """Sauberes Turn-Ende. **Alle Abschlussdaten stehen hier**, nicht in einem Seitenkanal —
    das ist der eigentliche Grund für dieses Format: vorher musste jeder Konsument wissen,
    wann welches Feld eines mitlaufenden Dicts gefüllt ist."""
    type = "done"
    stop_reason: str | None = None
    # Die vollständige Antwort, wie die CLI sie im result-Ereignis meldet. Die Deltas
    # davor ergeben dasselbe; dies ist die Quelle der Wahrheit, wenn der Delta-Strom
    # unvollständig war.
    text: str = ""
    usage: dict = field(default_factory=dict)
    cost: dict = field(default_factory=dict)
    timing: dict = field(default_factory=dict)


@dataclass
class Failed(Event):
    """Getrennt von `Done`, weil ein Fehler und ein sauberes Ende zwei Fälle sind.

    `upstream_status` trägt den echten Status der Gegenstelle, wo die CLI ihn nennt
    (`api_error_status`, z.B. 404 bei unbekanntem Modell)."""
    type = "failed"
    error_type: str = "api_error"
    message: str = ""
    upstream_status: int | None = None
    retryable: bool = False
    cost: dict = field(default_factory=lambda: cost(None, {}))


# ------------------------------------------------------------------ Projektionen

def usage(raw, thinking=None, thinking_basis=None):
    """CLI-Usage -> Wire-Usage.

    Die Cache-Aufteilung nach TTL (5m/1h) kommt aus `usage.cache_creation` und ist
    `None`, wo die CLI sie nicht mitschickt — nicht 0, das wäre eine Behauptung.
    """
    if thinking_basis not in (None, "reported", "estimated"):
        raise ValueError("thinking_basis must be reported, estimated, or None")
    if thinking is None:
        thinking_basis = None
    raw = raw or {}
    creation = raw.get("cache_creation") or {}
    new = raw.get("input_tokens") or 0
    cached = raw.get("cache_read_input_tokens") or 0
    written = raw.get("cache_creation_input_tokens") or 0
    return {
        # Siehe Modul-Docstring: 'new' enthält die Cache-Treffer NICHT.
        "input_new": new,
        "cache_read": cached,
        "cache_write": written,
        "cache_write_5m": creation.get("ephemeral_5m_input_tokens"),
        "cache_write_1h": creation.get("ephemeral_1h_input_tokens"),
        "input_total": new + cached + written,
        "output": raw.get("output_tokens") or 0,
        "thinking": thinking,
        "thinking_basis": thinking_basis,
        "service_tier": raw.get("service_tier"),
    }


def cost(total_usd, by_model, scope=None, covered_requests=None, epoch=None):
    """Nominale Kosten mit expliziter Zuordnungs-Coverage.

    `by_model` ist die Aufschlüsselung der CLI (`modelUsage`) und enthält gemessen auch
    **Fremdarbeit**: auf einem sonnet-Turn stand dort zusätzlich Haiku mit 522
    Input-Tokens für CLI-interne Nebenaufrufe. Die Werte sind API-Listenpreise und kein Maß
    für den Abo-Verbrauch.

    `scope` ist nur `call`, wenn genau dieser Request abgedeckt ist, oder `turn`, wenn das
    Delta mehrere Requests derselben expliziten Session abdeckt. Ohne sichere Zuordnung
    bleiben Scope und Anzahl null. `epoch` identifiziert den besitzenden CLI-Prozess und ist
    opak; nach einem beobachteten Kosten-Counter-Reset wechselt es.
    """
    if by_model is None:
        by_model = {}
    if not isinstance(by_model, dict):
        raise ValueError("by_model must be an object")
    if total_usd is not None and (not isinstance(total_usd, (int, float))
                                  or isinstance(total_usd, bool)
                                  or not math.isfinite(total_usd) or total_usd < 0):
        raise ValueError("total_usd must be a finite non-negative number or None")
    if epoch is not None:
        if not isinstance(epoch, str):
            raise ValueError("cost epoch must be a UUID string or None")
        try:
            parsed_epoch = uuid.UUID(epoch)
        except (ValueError, AttributeError):
            raise ValueError("cost epoch must be a UUID string or None") from None
        if str(parsed_epoch) != epoch:
            raise ValueError("cost epoch must use canonical UUID spelling")
    if total_usd is None:
        scope, covered_requests = None, None
    elif epoch is None:
        raise ValueError("known cost requires a cost epoch")
    if scope not in (None, "call", "turn"):
        raise ValueError("cost scope must be call, turn, or None")
    if covered_requests is not None and (not isinstance(covered_requests, int)
                                         or isinstance(covered_requests, bool)
                                         or covered_requests <= 0):
        raise ValueError("covered_requests must be a positive integer or None")
    if scope == "call" and covered_requests != 1:
        raise ValueError("call cost must cover exactly one request")
    if scope == "turn" and (covered_requests is None or covered_requests < 2):
        raise ValueError("turn cost must cover at least two requests")
    if scope is None:
        covered_requests = None
    return {"total_usd": total_usd, "by_model": by_model,
            "scope": scope, "covered_requests": covered_requests, "epoch": epoch}
