"""Konfiguration aus Umgebungsvariablen (keine Extra-Dependency)."""
import logging
import os
import tempfile


def _truthy(v) -> bool:
    return str(v).lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self) -> None:
        self.host = os.getenv("HOST", "127.0.0.1")
        self.port = int(os.getenv("PORT", "8000"))
        self.api_key = os.getenv("API_KEY") or None
        self.claude_bin = os.getenv("CLAUDE_BIN", "claude")
        self.default_model = os.getenv("DEFAULT_MODEL", "sonnet")
        self.request_timeout = float(os.getenv("REQUEST_TIMEOUT", "180"))
        # Perf-Hebel (opt-in). Leer = CLI-Default.
        self.effort = os.getenv("EFFORT") or None        # low | medium | high | xhigh | max
        self.system_prompt = os.getenv("SYSTEM_PROMPT") or None
        _spf = os.getenv("SYSTEM_PROMPT_FILE")
        if _spf and not self.system_prompt:
            try:
                with open(_spf, encoding="utf-8") as f:
                    self.system_prompt = f.read()
            except OSError:
                pass
        self.debug = _truthy(os.getenv("DEBUG", "0"))
        # CLI-stderr mitloggen (Default: an, wenn DEBUG).
        self.log_cli_stderr = _truthy(os.getenv("LOG_CLI_STDERR", "1" if self.debug else "0"))
        self.metrics_window = int(os.getenv("METRICS_WINDOW", "1000"))
        # Prozess-Pool (Reuse) — spart Spawn/Init-Overhead pro Request.
        self.pool_enabled = _truthy(os.getenv("POOL_ENABLED", "1"))
        self.pool_max_procs = int(os.getenv("POOL_MAX_PROCS", "8"))
        self.pool_idle_ttl = float(os.getenv("POOL_IDLE_TTL", "180"))     # s idle -> sterben
        self.pool_max_uses = int(os.getenv("POOL_MAX_USES", "100"))       # danach recyceln
        self.pool_reap_interval = float(os.getenv("POOL_REAP_INTERVAL", "30"))
        self.clear_timeout = float(os.getenv("CLEAR_TIMEOUT", "15"))       # /clear ist instant; kurz halten
        # Nach außen exponierte Modell-IDs (die CLI akzeptiert diese Aliase direkt).
        self.models = ["sonnet", "opus", "haiku"]
        self.known_models = set(self.models) | {"default"}
        # Neutrales Arbeitsverzeichnis, damit die CLI kein CLAUDE.md/Projekt aufsammelt.
        self.workdir = os.getenv("WORKDIR") or tempfile.mkdtemp(prefix="claude-proxy-")


settings = Settings()

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
