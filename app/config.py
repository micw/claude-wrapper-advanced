"""Konfiguration aus Umgebungsvariablen (keine Extra-Dependency)."""
import logging
import os
import tempfile


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _truthy(v) -> bool:
    return str(v).lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------- Modelle
# Endliche, handgepflegte Liste. Nach außen ohne 'claude-'-Präfix; an die CLI geht
# IMMER der volle Name, nie ein Alias: CLI-Aliase driften mit der CLI-Version
# (auf 2.1.198 zeigt 'opus' noch auf Opus 4.8, nicht auf Opus 5).
# Reihenfolge = Rang, der erste Eintrag je Familie ist "das neueste".
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
_EFF5 = EFFORT_ORDER
_EFF4 = ("low", "medium", "high", "max")      # 'xhigh' gibt es erst ab Opus 4.7
DEFAULT_EFFORT = "high"                       # CLI-Default auf allen Modellen außer Opus 4.7

# Felder: (CLI-Modell, Anzeigename, Kontext, Effort-Stufen, knowledge-cutoff).
# Cutoff wird im Replace-Modus mitgegeben, weil der CLI-Default — der ihn sonst pro Modell
# liefert — dann weg ist und das Modell seine eigene Grenze sonst ~1 Jahr zu früh rät.
# Werte aus dem CLI-Default abgelesen (tests/assumptions.py::model.registry gleicht sie ab);
# None = die CLI nennt für dieses Modell keinen Cutoff (z.B. Opus 5) -> wir nennen auch keinen.
# Pflege bei Modell-Releases, dieselbe Kadenz wie die Modell-Liste selbst.
MODELS = {
    # externe ID     CLI-Modell             Anzeige       Kontext    Effort      Cutoff
    "opus-5":     ("claude-opus-5",     "Opus 5",     1_000_000, _EFF5, None),
    "opus-4-8":   ("claude-opus-4-8",   "Opus 4.8",   1_000_000, _EFF5, "January 2026"),
    "sonnet-5":   ("claude-sonnet-5",   "Sonnet 5",   1_000_000, _EFF5, "January 2026"),
    "fable-5":    ("claude-fable-5",    "Fable 5",    1_000_000, _EFF5, "January 2026"),
    "sonnet-4-6": ("claude-sonnet-4-6", "Sonnet 4.6", 1_000_000, _EFF4, "August 2025"),
    "haiku-4-5":  ("claude-haiku-4-5",  "Haiku 4.5",    200_000, (),    "February 2025"),
}

ALIASES = {"opus": "opus-5", "sonnet": "sonnet-5",
           "haiku": "haiku-4-5", "fable": "fable-5"}
# 'best' bewusst nicht: der CLI-Alias bedeutet "Fable, wo verfügbar, sonst neuestes Opus" —
# eine statische Abbildung auf fable-5 verliert diese Bedeutung und dupliziert nur 'fable'.

# Leiter von Absichten statt Kreuzprodukt: jeder Eintrag beantwortet "warum der und
# nicht der daneben". Kleines Modell auf hoher Stufe fehlt bewusst — dafür gibt es
# das größere Modell auf Default.
DEFAULT_EFFORT_PICKS = "sonnet:low,opus:medium,opus:xhigh,opus:max"


def _parse_picks(raw):
    picks = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        model, _, eff = item.rpartition(":")
        picks.append((model, eff))
    return picks


def clamp_effort(eff, levels):
    """Operator-Default auf die höchste vom Modell unterstützte Stufe <= eff senken.

    Spiegelt das dokumentierte CLI-Verhalten ('xhigh' läuft als 'high' auf Opus 4.6).
    Gilt nur für den Env-Default — ein Client-Wunsch wird stattdessen mit 400 abgelehnt.
    """
    if not eff or not levels:
        return None
    if eff in levels:
        return eff
    want = EFFORT_ORDER.index(eff) if eff in EFFORT_ORDER else len(EFFORT_ORDER)
    below = [l for l in levels if EFFORT_ORDER.index(l) <= want]
    return below[-1] if below else None


class Settings:
    def __init__(self) -> None:
        self.host = os.getenv("HOST", "127.0.0.1")
        self.port = int(os.getenv("PORT", "8000"))
        self.api_key = os.getenv("API_KEY") or None
        self.claude_bin = os.getenv("CLAUDE_BIN", "claude")
        self.default_model = os.getenv("DEFAULT_MODEL", "sonnet")
        # Explizite HTTP-Body-Grenze, passend zu nginx `client_max_body_size 32m`.
        # Starlette puffert Request.json() sonst ohne eigene Obergrenze; direkte Zugriffe auf
        # den Wrapper könnten damit die Proxy-Grenze umgehen.
        self.max_request_body_bytes = int(os.getenv("MAX_REQUEST_BODY_BYTES", str(32 << 20)))
        # Zwei Uhren: die Idle-Schwelle ist die eigentliche Grenze (Stille zwischen zwei
        # Stream-Zeilen), REQUEST_TIMEOUT nur noch die Reißleine für den Gesamt-Turn.
        # Eine reine Gesamtfrist würde laufende Antworten abschneiden: opus/xhigh denkt
        # minutenlang, und die CLI ist dabei selbst bei 1 MB Kontext nie länger als ~10 s
        # still (Prefill; sonst < 2 s) — gemessen, siehe cli.streams_continuously.
        self.idle_timeout = float(os.getenv("IDLE_TIMEOUT", "60"))
        self.request_timeout = float(os.getenv("REQUEST_TIMEOUT", "1800"))
        # Zeilenpuffer der CLI-Streams. asyncio.StreamReader hat 64 KiB Default — die Deltas
        # sind klein, aber das finale assistant/result-Event trägt die GANZE Antwort auf EINER
        # Zeile (~15k Output-Token JSON-escaped reichen schon). Prod 2026-08-26: zwei Requests
        # starben daran mitten im Stream. Der Wert ist eine Puffer-Obergrenze PRO Reader, also
        # im Worst Case STREAM_LIMIT x POOL_MAX_PROCS Speicher — nicht beliebig hochdrehen.
        self.stream_limit = int(os.getenv("STREAM_LIMIT", str(16 << 20)))
        # Perf-Hebel (opt-in). Leer = CLI-Default.
        self.effort = os.getenv("EFFORT") or None        # low | medium | high | xhigh | max
        # Basis-System-Prompt: ERSETZT im Replace-Modus (Default) den CLI-Default via
        # --system-prompt. Der Default (je Modell 1,4k-6,6k Token) beschreibt eine Terminal-/Tool-
        # Umgebung, die hier nicht existiert. Ersetzen entfernt aber auch dessen Modell-Identität
        # und knowledge-cutoff-Zeile -> die geben wir pro Modell selbst mit (build_system_prompt).
        # REPLACE_SYSTEM_PROMPT=0 schaltet das ab: dann fassen wir den Default gar nicht an (kein
        # replace, kein append unserer Basis) — NUR ein externer Client-System-Prompt wird immer
        # via --append-system-prompt angehängt.
        self.replace_system_prompt = _truthy(os.getenv("REPLACE_SYSTEM_PROMPT", "1"))
        self.system_prompt = os.getenv("SYSTEM_PROMPT") or None
        # Die gebündelte chat.txt ist der DEFAULT — sonst müsste jede Deployment-Umgebung (k8s,
        # docker run, ...) SYSTEM_PROMPT_FILE selbst setzen, und ohne sie fiele man still auf den
        # großen CLI-Default zurück. Wer den Basis-Prompt NICHT will, setzt REPLACE_SYSTEM_PROMPT=0
        # (build_system_prompt liefert dann None) oder SYSTEM_PROMPT_FILE="".
        _spf = os.getenv("SYSTEM_PROMPT_FILE", "system-prompts/chat.txt")
        if _spf and not self.system_prompt:
            # Relative Pfade gegen das Repo-Root (Parent von app/) auflösen, nicht gegen den
            # CWD des Servers — der ist unter uvicorn/Docker nicht garantiert das Repo.
            path = _spf if os.path.isabs(_spf) else os.path.join(_REPO_ROOT, _spf)
            try:
                with open(path, encoding="utf-8") as f:
                    self.system_prompt = f.read()
            except OSError:
                logging.getLogger("config").warning(
                    "SYSTEM_PROMPT_FILE=%s nicht lesbar (aufgelöst: %s) — CLI-Default wird benutzt",
                    _spf, path)
        # Thinking-Fortschritt als reasoning_content mitstreamen. Die CLI liefert KEINEN Denktext
        # (thinking ist leer, siehe cli.thinking_is_redacted) — nur estimated_tokens pro Event.
        # Daraus bauen wir eine Fortschrittszeile, sonst schweigt der Stream minutenlang.
        self.stream_thinking = _truthy(os.getenv("STREAM_THINKING", "1"))
        self.thinking_interval = float(os.getenv("THINKING_INTERVAL", "10"))  # s zwischen Updates
        # /v1/responses aktualisiert die Zeile in place (summary_part.done ERSETZT), statt sie
        # anzuhängen — dort ist ein kurzes Intervall sinnvoll statt einer Textwand.
        self.thinking_interval_responses = float(os.getenv("THINKING_INTERVAL_RESPONSES", "1"))
        self.debug = _truthy(os.getenv("DEBUG", "0"))
        # CLI-stderr mitloggen (Default: an, wenn DEBUG).
        self.log_cli_stderr = _truthy(os.getenv("LOG_CLI_STDERR", "1" if self.debug else "0"))
        self.metrics_window = int(os.getenv("METRICS_WINDOW", "1000"))
        # History-Caching: User-Message als Content-Array [History+cache_control, Tail] senden,
        # damit die (append-stabile) History inkrementell gecacht wird (nur neuer Turn = fresh input).
        # Empirisch: braucht die ttl-Form wie der CLI-System-Block; ohne ttl verwirft die CLI die Message.
        self.cache_history = _truthy(os.getenv("CACHE_HISTORY", "1"))
        self.cache_history_ttl = os.getenv("CACHE_HISTORY_TTL", "1h")     # 1h | 5m
        # Bilder (Vision): Limits des Backends — größere/mehr Bilder werden verworfen
        # und in der History als Hinweis vermerkt, statt den Request mit 400 zu killen.
        self.max_image_mb = float(os.getenv("MAX_IMAGE_MB", "5"))
        self.max_image_bytes = int(self.max_image_mb * 1024 * 1024)
        self.max_images = int(os.getenv("MAX_IMAGES", "20"))
        # Prozess-Pool (Reuse) — spart Spawn/Init-Overhead pro Request.
        self.pool_enabled = _truthy(os.getenv("POOL_ENABLED", "1"))
        self.pool_max_procs = int(os.getenv("POOL_MAX_PROCS", "8"))
        self.pool_idle_ttl = float(os.getenv("POOL_IDLE_TTL", "180"))     # s idle -> sterben
        self.pool_max_uses = int(os.getenv("POOL_MAX_USES", "100"))       # danach recyceln
        self.pool_reap_interval = float(os.getenv("POOL_REAP_INTERVAL", "30"))
        self.clear_timeout = float(os.getenv("CLEAR_TIMEOUT", "15"))       # /clear ist instant; kurz halten
        # Modell-Registry: endliche Liste, siehe MODELS/ALIASES unten.
        self.models = MODELS
        self.aliases = ALIASES
        # Picker-Einträge mit festgenagelter Effort-Stufe ('opus:medium'). Leer = keine.
        # Kein ':high' — das ist der Default und wäre ein Duplikat der nackten ID.
        self.effort_picks = _parse_picks(os.getenv("EFFORT_PICKS", DEFAULT_EFFORT_PICKS))
        # Neutrales Arbeitsverzeichnis, damit die CLI kein CLAUDE.md/Projekt aufsammelt.
        self.workdir = os.getenv("WORKDIR") or tempfile.mkdtemp(prefix="claude-proxy-")


settings = Settings()

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
