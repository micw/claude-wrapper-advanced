"""Leichtgewichtige In-Memory-Metriken (für Latenz-/Durchsatz-Debugging)."""
import time
from collections import defaultdict, deque

from .config import settings


def _pct(sorted_vals, p):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, int(p / 100 * len(sorted_vals)))
    return round(sorted_vals[i], 1)


class Metrics:
    def __init__(self, window: int):
        self.counts = defaultdict(int)          # outcome -> count
        self.total = deque(maxlen=window)        # ms end-to-end (unsere Wall-Clock)
        self.ttft = deque(maxlen=window)         # ms bis erstes Token/Event
        self.spawn = deque(maxlen=window)        # ms für CLI-Prozess-Spawn (nur fork)
        self.cli_dur = deque(maxlen=window)      # ms CLI-interne Dauer (aus result-Event)
        self.overhead = deque(maxlen=window)     # ms total - cli_internal (pool-sparbar)
        self.inflight = 0
        self.total_requests = 0
        self.started = time.time()
        # Cache-Statistik (kumulativ)
        self.cache_read = 0
        self.cache_write = 0
        self.prompt_toks = 0
        # Kontingent, pro Gruppe akkumuliert (aus rate_limit_event). Siehe update_rate_limit().
        self.limit_groups = {}
        self.limit_state = None

    def update_rate_limit(self, info):
        """Ein rate_limit_event einsortieren.

        Je Turn nennt das Backend GENAU EIN Fenster (`rateLimitType`, aus dem Header
        `anthropic-ratelimit-unified-representative-claim`). Welches, entscheidet es selbst —
        gemessen kam `five_hour`, während das Wochenfenster mit 72 % deutlich voller war. Der
        Wert ist also NICHT "die Gruppe, die dieser Turn belastet hat"; er heißt hier deshalb
        representative_claim und nicht active_group.

        Daraus folgt die Akkumulation: ein einzelner Slot würde den five_hour-Stand verwerfen,
        sobald einmal ein seven_day-Ereignis kommt. Jede Gruppe behält ihren eigenen letzten
        Stand, mit eigenem Zeitstempel — ein alter Wert ist eine Information, ein
        überschriebener ist keine.

        `utilization` fehlt im Normalfall: das Backend schickt es nur bei `allowed_warning`
        und beim 429 (gemessen). Der Füllstand kommt aus GET /api/oauth/usage, nicht von hier.
        """
        if not info:
            return
        now = int(time.time())
        kind = info.get("rateLimitType")
        if kind:
            util = info.get("utilization")
            self.limit_groups[kind] = {
                "status": info.get("status"),
                # utilization kommt als Bruch 0..1; die Usage-API liefert Prozent. Hier wird
                # auf Prozent normalisiert, damit beide Quellen dieselbe Einheit sprechen.
                "used_percent": round(util * 100, 2) if isinstance(util, (int, float)) else None,
                "resets_at": info.get("resetsAt"),
                "reached": info.get("status") == "rejected",
                "updated_at": now,
            }
        self.limit_state = {
            "representative_claim": kind,
            "status": info.get("status"),
            "surpassed_threshold": info.get("surpassedThreshold"),
            "overage": {
                "status": info.get("overageStatus"),
                "in_use": info.get("isUsingOverage"),
                "disabled_reason": info.get("overageDisabledReason"),
                "error_code": info.get("errorCode"),
            },
            "updated_at": now,
        }

    def start(self):
        self.inflight += 1
        self.total_requests += 1

    def end(self, outcome, total_ms=None, ttft_ms=None, spawn_ms=None, cli_dur_ms=None, usage=None):
        self.inflight = max(0, self.inflight - 1)
        self.counts[outcome or "unknown"] += 1
        if usage:
            ptd = usage.get("prompt_tokens_details") or {}
            self.cache_read += ptd.get("cached_tokens") or 0
            self.cache_write += ptd.get("cache_write_tokens") or 0
            self.prompt_toks += usage.get("prompt_tokens") or 0
        if total_ms is not None:
            self.total.append(total_ms)
        if ttft_ms is not None:
            self.ttft.append(ttft_ms)
        if spawn_ms is not None:
            self.spawn.append(spawn_ms)
        if cli_dur_ms is not None:
            self.cli_dur.append(cli_dur_ms)
            if total_ms is not None:
                self.overhead.append(max(0.0, total_ms - cli_dur_ms))

    def snapshot(self):
        done = sum(self.counts.values())
        errors = self.counts.get("timeout", 0) + self.counts.get("error", 0)

        limits = None
        if self.limit_state:
            now = int(time.time())
            groups = {}
            for kind, g in self.limit_groups.items():
                g = dict(g)
                if g.get("resets_at"):
                    g["resets_in_s"] = max(0, g["resets_at"] - now)
                groups[kind] = g
            limits = {**self.limit_state, "groups": groups}

        def band(d):
            s = sorted(d)
            return {"p50": _pct(s, 50), "p95": _pct(s, 95), "p99": _pct(s, 99), "n": len(s)}

        return {
            "uptime_s": round(time.time() - self.started, 1),
            "total_requests": self.total_requests,
            "inflight": self.inflight,
            "outcomes": dict(self.counts),
            "error_rate": round(errors / done, 4) if done else 0.0,
            "cache": {
                "hit_rate": round(self.cache_read / self.prompt_toks, 4) if self.prompt_toks else 0.0,
                "read_tokens": self.cache_read,     # Cache-Hits (billig/schnell)
                "write_tokens": self.cache_write,   # Cache-Writes (teuer, einmalig pro Präfix)
                "prompt_tokens": self.prompt_toks,
            },
            "latency_ms": {
                "total": band(self.total),      # inkl. Spawn + Inferenz
                "ttft": band(self.ttft),        # bis erstes Token
                "spawn": band(self.spawn),      # nur Prozess-Fork (nicht CLI-Init)
                "cli_internal": band(self.cli_dur),  # CLI-eigene Messung
                "overhead": band(self.overhead),     # total - cli_internal = pool-sparbar
            },
            # Kontingent: pro Gruppe der letzte bekannte Stand, dazu der zuletzt gemeldete
            # Gesamtzustand. Füllstände nur, wo das Backend sie mitschickt (Warnung/429).
            "limits": limits,
        }


metrics = Metrics(settings.metrics_window)
