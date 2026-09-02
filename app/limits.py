"""Subscription quota from official Claude CLI turn response headers.

A setup-token can run inference but cannot call ``GET /api/oauth/usage`` (measured 403,
missing ``user:profile``). Every ``/v1/messages`` response nevertheless carries the quota
windows. A fail-open Bun preload forwards only those headers over a dedicated fd.

Normal turns refresh this cache for free. ``usage()`` starts a tiny real CLI probe only
when a group's observation exceeds its configured MAX_AGE, or explicitly through
``force=global|fable-5|all``. The public model deliberately contains measurements, reset
and age — no status/severity policy that belongs to the consumer.
"""
import asyncio
import contextlib
import logging
import time

from .config import settings

log = logging.getLogger("limits")

WINDOW_SECONDS = {"five_hour": 18000, "seven_day": 604800}
ACCOUNT_WIDE = ("five_hour", "seven_day")


class UsageUnavailable(Exception):
    """No current or previously observed quota state is available."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


# ------------------------------------------------------------------ identities

def model_key(name):
    """CLI/display/concrete model name -> stable external registry id."""
    if not name:
        return None
    n = name.strip().lower()
    if n.startswith("claude-"):
        n = n[len("claude-"):]
    # Internal side calls can use a dated concrete id such as haiku-4-5-20251001.
    for key, spec in settings.models.items():
        cli = spec[0].lower().removeprefix("claude-")
        if n == cli or n.startswith(cli + "-"):
            return key
    for cand in (n, n.replace(" ", "-"), n.split()[0] if n.split() else n):
        if cand in settings.models:
            return cand
        if cand in settings.aliases:
            return settings.aliases[cand]
    return n.replace(" ", "-")


def _duration(claim):
    for known in ("seven_day", "five_hour"):
        if claim == known or claim.startswith(known + "_"):
            return known
    return claim


def window_key(claim, model=None):
    """CLI rate-limit claim -> stable window key used by limit_status."""
    if not claim:
        return None
    if claim in ACCOUNT_WIDE:
        return f"global/{claim}"
    key = model_key(model)
    return f"model:{key}/{_duration(claim)}" if key else None


# -------------------------------------------------------- alarm from CLI stream

def status_from_event(info, model=None):
    """rate_limit_event -> alarm, or None for the normal allowed case.

    This remains separate from quota snapshots. The latter intentionally carry no status.
    """
    if not info:
        return None
    status = info.get("status")
    overage = info.get("overageStatus")
    if (status in (None, "allowed")
            and not info.get("isUsingOverage")
            and not info.get("errorCode")):
        return None
    claim = info.get("rateLimitType")
    return {
        "window": window_key(claim, model),
        "claim": claim,
        "status": status,
        "resets_at": info.get("resetsAt"),
        "surpassed_threshold": info.get("surpassedThreshold"),
        "overage": {
            "status": overage,
            "in_use": info.get("isUsingOverage"),
            "disabled_reason": info.get("overageDisabledReason"),
            "error_code": info.get("errorCode"),
        },
        "usage_stale": True,
    }


# ---------------------------------------------------- response-header snapshots

_HEADER_PREFIX = "anthropic-ratelimit-unified-"
_FABLE_MODELS = tuple(
    model for model in settings.models
    if "fable" in settings.model_limit_groups.get(model, ())
)
_GROUP_SPECS = {
    "global": {
        "upstream_id": None,
        "scope": None,
        "windows": (("five_hour", "5h", 18000), ("seven_day", "7d", 604800)),
    },
    "model:fable-5": {
        "upstream_id": "7d_oi",
        "scope": {"family": "fable", "models": list(_FABLE_MODELS)},
        "windows": (("seven_day", "7d_oi", 604800),),
    },
}
_observed = {
    gid: {"observed_at": None, "windows": {}, "revision": 0}
    for gid in _GROUP_SPECS
}
_probe_inflight = {"global": None, "fable-5": None}


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _header_window(headers, upstream_id):
    used = _number(headers.get(f"{_HEADER_PREFIX}{upstream_id}-utilization"))
    reset = _integer(headers.get(f"{_HEADER_PREFIX}{upstream_id}-reset"))
    if used is None or reset is None or not 0 <= used <= 1:
        return None
    return {"used_percent": round(used * 100, 2), "resets_at": reset}


def _merge_window(old, new):
    if not old or new["resets_at"] > old["resets_at"]:
        return new
    if new["resets_at"] < old["resets_at"]:
        return old
    # Concurrent requests can straddle a percentage boundary and complete out of order.
    return {**new, "used_percent": max(old["used_percent"], new["used_percent"])}


def observe_turn_headers(headers, model=None, now=None):
    """Merge one Messages response and return the complete snapshot when valid."""
    if not isinstance(headers, dict):
        return None
    headers = {str(k).lower(): v for k, v in headers.items()}
    now = int(time.time() if now is None else now)
    changed = False

    global_updates = {}
    for wid, upstream, _seconds in _GROUP_SPECS["global"]["windows"]:
        value = _header_window(headers, upstream)
        if value:
            global_updates[wid] = value
    # Group age promises that both global windows were observed together.
    if len(global_updates) == len(_GROUP_SPECS["global"]["windows"]):
        state = _observed["global"]
        for wid, value in global_updates.items():
            state["windows"][wid] = _merge_window(state["windows"].get(wid), value)
        state["observed_at"] = now
        state["revision"] += 1
        changed = True

    if model_key(model) in _FABLE_MODELS:
        value = _header_window(headers, "7d_oi")
        if value:
            state = _observed["model:fable-5"]
            state["windows"]["seven_day"] = _merge_window(
                state["windows"].get("seven_day"), value)
            state["observed_at"] = now
            state["revision"] += 1
            changed = True

    return quota_snapshot(now) if changed else None


def quota_snapshot(now=None):
    now = int(time.time() if now is None else now)
    groups = []
    for gid, spec in _GROUP_SPECS.items():
        state = _observed[gid]
        observed_at = state["observed_at"]
        windows = []
        for wid, upstream, seconds in spec["windows"]:
            value = state["windows"].get(wid) or {}
            windows.append({
                "id": wid,
                "upstream_id": upstream,
                "used_percent": value.get("used_percent"),
                "window_seconds": seconds,
                "resets_at": value.get("resets_at"),
            })
        groups.append({
            "id": gid,
            "upstream_id": spec["upstream_id"],
            "scope": spec["scope"],
            "observed_at": observed_at,
            "age_seconds": max(0, now - observed_at) if observed_at is not None else None,
            "windows": windows,
        })
    return {"groups": groups}


def _age(group, now):
    observed_at = _observed[group]["observed_at"]
    return None if observed_at is None else max(0, now - observed_at)


def _reset_observations():
    """Test seam; production never discards a known observation."""
    for state in _observed.values():
        state["observed_at"] = None
        state["windows"].clear()
        state["revision"] = 0


# ---------------------------------------------------------------------- probes

async def _probe(kind):
    """Run the smallest real CLI turn. A Fable response refreshes both groups."""
    model_id = "haiku-4-5" if kind == "global" else _FABLE_MODELS[0]
    cli_model = settings.models[model_id][0]
    target = "global" if kind == "global" else "model:fable-5"
    before = _observed[target]["revision"]
    args = [
        "-p", "Reply only: OK",
        "--model", cli_model,
        "--system-prompt", "",
        "--safe-mode",
        "--tools", "",
        "--output-format", "json",
        "--no-session-persistence",
    ]
    from .cli_driver import spawn_cli
    proc = await spawn_cli(args, cli_model, stdin=asyncio.subprocess.DEVNULL)
    try:
        try:
            _out, err = await asyncio.wait_for(proc.communicate(), settings.usage_probe_timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise UsageUnavailable(f"{kind} usage probe timed out") from None
        capture = getattr(proc, "turn_header_capture", None)
        if capture is not None:
            await capture.close()  # pipe EOF guarantees all response events were consumed
        if proc.returncode != 0:
            detail = (err or b"")[-300:].decode(errors="replace")
            raise UsageUnavailable(f"{kind} usage probe exited {proc.returncode}: {detail}")
        if _observed[target]["revision"] == before:
            raise UsageUnavailable(f"{kind} usage probe returned no quota headers")
    finally:
        capture = getattr(proc, "turn_header_capture", None)
        if capture is not None:
            with contextlib.suppress(Exception):
                await capture.close()


async def _run_probe(kind):
    """Singleflight per probe kind."""
    task = _probe_inflight[kind]
    if task is None or task.done():
        task = asyncio.create_task(_probe(kind))
        _probe_inflight[kind] = task
    try:
        await asyncio.shield(task)
    finally:
        if _probe_inflight[kind] is task and task.done():
            _probe_inflight[kind] = None


async def usage(force=None):
    """Return quota, refreshing groups by age or explicit force.

    ``force`` is None, ``global``, ``fable-5``, or ``all``. Fable covers every
    currently known group, so ``all`` needs only that one probe.
    """
    if force not in (None, "global", "fable-5", "all"):
        raise ValueError("force must be one of: global, fable-5, all")

    if force == "global":
        await _run_probe("global")
        return quota_snapshot()
    if force in ("fable-5", "all"):
        await _run_probe("fable-5")
        return quota_snapshot()

    now = int(time.time())
    global_age = _age("global", now)
    fable_age = _age("model:fable-5", now)
    global_due = global_age is None or global_age >= settings.usage_global_max_age
    fable_due = fable_age is None or fable_age >= settings.usage_fable_max_age
    try:
        if fable_due:
            await _run_probe("fable-5")  # also global; never run both
        elif global_due:
            await _run_probe("global")
    except UsageUnavailable:
        # Age itself communicates staleness. Keep known data; fail only on a cold start.
        if _observed["global"]["observed_at"] is None:
            raise
        log.warning("usage probe failed; returning the last observed quota", exc_info=True)
    return quota_snapshot()
