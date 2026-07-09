"""Nicht-interaktiver Auth-Status der Claude CLI (`claude auth status --json`).

Wird für /healthz und den Chat-Endpoint genutzt, damit ein nicht-eingeloggter
Container klar antwortet ("erst `claude /login` im Container") statt kryptischer CLI-Fehler.
Ergebnis wird kurz gecacht, damit nicht jeder Request einen Subprozess spawnt; nach einem
Login im laufenden Container erholt sich der Status innerhalb der TTL.
"""
import asyncio
import contextlib
import json
import logging
import time

from .config import settings

log = logging.getLogger("auth")

_TTL_OK = 30.0        # eingeloggt: selten neu prüfen
_TTL_FAIL = 5.0       # ausgeloggt: schnell erholen, sobald jemand /login macht
_cache = {"at": 0.0, "val": None}
_lock = asyncio.Lock()


async def _probe():
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.claude_bin, "auth", "status", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except FileNotFoundError:
        return {"loggedIn": False, "error": f"claude binary '{settings.claude_bin}' not found"}
    except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
        with contextlib.suppress(Exception):
            proc.kill()
        return {"loggedIn": False, "error": f"auth check failed: {e}"}
    try:
        return json.loads(out.decode(errors="replace"))
    except Exception:
        return {"loggedIn": False, "error": "could not parse auth status"}


async def auth_status(force: bool = False) -> dict:
    now = time.monotonic()
    val = _cache["val"]
    if not force and val is not None:
        ttl = _TTL_OK if val.get("loggedIn") else _TTL_FAIL
        if now - _cache["at"] < ttl:
            return val
    async with _lock:
        # Doppel-Check nach dem Lock (anderer Coroutine könnte gerade aktualisiert haben).
        now = time.monotonic()
        val = _cache["val"]
        if not force and val is not None:
            ttl = _TTL_OK if val.get("loggedIn") else _TTL_FAIL
            if now - _cache["at"] < ttl:
                return val
        val = await _probe()
        _cache["val"] = val
        _cache["at"] = time.monotonic()
        return val
