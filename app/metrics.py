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
        # Letzter Rate-Limit-Stand (account-weit, aus rate_limit_event)
        self.rate_limit = None

    def update_rate_limit(self, info):
        if not info:
            return
        self.rate_limit = {
            "status": info.get("status"),
            "type": info.get("rateLimitType"),
            "resets_at": info.get("resetsAt"),
            "using_overage": info.get("isUsingOverage"),
            "updated_at": int(time.time()),
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
            self.cache_write += ptd.get("cache_creation_tokens") or 0
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

        rl = None
        if self.rate_limit:
            rl = dict(self.rate_limit)
            if rl.get("resets_at"):
                rl["resets_in_s"] = max(0, rl["resets_at"] - int(time.time()))

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
            "rate_limit": rl,   # account-weit: status/type/resets_in_s (aus rate_limit_event)
        }


metrics = Metrics(settings.metrics_window)
