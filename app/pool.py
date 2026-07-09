"""Prozess-Pool: hält lebende CLI-Prozesse, wiederverwendet sie über /clear.

- Gebucketet nach (Modell + Toolset), weil Tools/Modell beim Spawn fixiert sind.
- Reuse: /clear (Kontext-Reset, token-frei) + Prompt; Tool-Calls via Interrupt-statt-Kill.
- Warm-at-init: /clear direkt beim Spawn (löst die ~0.8s Init token-frei aus).
- Eviction: Idle-TTL + Max-Uses; bei Kapazität LRU-Eviction einer idle-Instanz.
"""
import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections import defaultdict, deque

from .config import settings
from .cli_driver import _build_args, classify

log = logging.getLogger("pool")


def _key(model, mcp_tools, effort) -> str:
    # effort ist ein Spawn-Zeit-Flag (--effort) -> muss in den Bucket, sonst würde eine
    # z.B. mit high gespawnte Instanz für einen low-Request recycelt.
    raw = model + "|" + (effort or "") + "|" + json.dumps(mcp_tools, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _clear_msg():
    return {"type": "user", "message": {"role": "user", "content": "/clear"}}


def _user_msg(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


_INTERRUPT = {"type": "control_request", "request_id": "i", "request": {"subtype": "interrupt"}}


class Proc:
    def __init__(self, key, args):
        self.key = key
        self.args = args
        self.proc = None
        self.stderr_tail = deque(maxlen=50)
        self._err_task = None
        self.uses = 0
        self.last_used = time.monotonic()
        self.dead = False
        self._cum_cost = 0.0   # total_cost_usd der CLI ist kumulativ pro Prozess

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            settings.claude_bin, *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=settings.workdir,
        )
        self._err_task = asyncio.create_task(self._drain_err())
        # Warm-up: /clear löst die CLI-Init aus (token-frei) und macht die Instanz nutzbar.
        await self._send(_clear_msg())
        await self._await_result(settings.request_timeout)

    async def _drain_err(self):
        with contextlib.suppress(Exception):
            async for line in self.proc.stderr:
                s = line.decode(errors="replace").rstrip()
                if s:
                    self.stderr_tail.append(s)

    async def _send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        with contextlib.suppress(Exception):
            await self.proc.stdin.drain()

    async def _await_result(self, timeout):
        """Liest bis zum nächsten result-Event; verwirft alles davor."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            raw = await asyncio.wait_for(self.proc.stdout.readline(), timeout=remaining)
            if not raw:
                raise EOFError("proc exited")
            try:
                m = json.loads(raw)
            except Exception:
                continue
            if m.get("type") == "result":
                return m

    async def _drain_after_interrupt(self):
        try:
            await self._await_result(settings.clear_timeout)
        except (asyncio.TimeoutError, EOFError):
            self.dead = True  # konnte nicht sauber in den Idle-Zustand -> verwerfen

    async def run_turn(self, prompt, stats):
        """Ein Turn auf dieser (wiederverwendeten) Instanz. Async-Generator."""
        self.uses += 1
        stats["reused"] = self.uses > 1
        t0 = time.perf_counter()
        loop = asyncio.get_running_loop()

        # Kontext-Reset
        try:
            await self._send(_clear_msg())
            await self._await_result(settings.clear_timeout)
        except (asyncio.TimeoutError, EOFError):
            self.dead = True
            stats["outcome"] = "error"
            yield ("error", {"type": "cli_exit", "message": "pooled proc unresponsive on /clear"})
            return

        await self._send(_user_msg(prompt))

        first = [False]

        def mark():
            if not first[0]:
                stats["ttft_ms"] = (time.perf_counter() - t0) * 1000
                first[0] = True

        deadline = loop.time() + settings.request_timeout
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    self.dead = True
                    stats["outcome"] = "timeout"
                    yield ("error", {"type": "timeout",
                                     "message": f"CLI timeout after {settings.request_timeout:.0f}s"})
                    return
                try:
                    raw = await asyncio.wait_for(self.proc.stdout.readline(), timeout=remaining)
                except asyncio.TimeoutError:
                    self.dead = True
                    stats["outcome"] = "timeout"
                    yield ("error", {"type": "timeout", "message": "CLI timeout"})
                    return
                if not raw:
                    self.dead = True
                    stats["outcome"] = "error"
                    tail = "\n".join(self.stderr_tail)
                    log.error("pooled proc died mid-turn. stderr:\n%s", tail or "(leer)")
                    yield ("error", {"type": "cli_exit", "message": tail[-500:] or "proc exited"})
                    return
                try:
                    m = json.loads(raw)
                except Exception:
                    continue
                ev = classify(m, stats, mark)
                if ev is None:
                    continue
                kind = ev[0]
                if kind == "delta":
                    yield ev
                elif kind == "tool_use":
                    # Turn abbrechen (Prozess bleibt am Leben), Client bekommt den Call sofort.
                    # Known limitation: total_cost_usd steht NUR im result-Event, nicht in der
                    # assistant-Message -> die (nominalen) Kosten dieses Tool-Turns erscheinen erst
                    # im Delta des nächsten vollen Turns. Per-Request-cost bei Tool-Paaren also
                    # verzerrt (kumulativ korrekt). Egal solange Abo/Usage-Limit statt echter Abrechnung.
                    await self._send(_INTERRUPT)
                    yield ev
                    await self._drain_after_interrupt()
                    return
                else:  # result / error
                    # Kumulative CLI-Kosten -> Per-Turn-Delta umrechnen.
                    if ev[0] == "result" and stats.get("cost_usd") is not None:
                        total_cost = stats["cost_usd"]
                        stats["cost_usd"] = max(0.0, total_cost - self._cum_cost)
                        self._cum_cost = total_cost
                    yield ev
                    return
        finally:
            self.last_used = time.monotonic()

    async def close(self):
        if self._err_task:
            self._err_task.cancel()
        with contextlib.suppress(Exception):
            self.proc.kill()
        with contextlib.suppress(Exception):
            await self.proc.wait()


class Pool:
    def __init__(self):
        self.idle = defaultdict(list)   # key -> [Proc]
        self.total = 0                  # lebende Prozesse (idle + busy)
        self.cond = asyncio.Condition()
        self._reaper = None
        self._closing = False

    async def acquire(self, model, mcp_tools, effort=None):
        key = _key(model, mcp_tools, effort)
        args = _build_args(mcp_tools, model, effort)
        async with self.cond:
            while True:
                lst = self.idle.get(key)
                if lst:
                    p = lst.pop()
                    # Liveness-Check: idle gecrashte Instanz nicht rausgeben (sonst scheitert
                    # der nächste Turn am /clear). Verwerfen und weitersuchen/spawnen.
                    if p.dead or (p.proc and p.proc.returncode is not None):
                        self.total -= 1
                        asyncio.create_task(p.close())
                        continue
                    return key, p, True
                if self.total < settings.pool_max_procs:
                    self.total += 1
                    break
                victim = self._pick_victim()
                if victim is not None:
                    self._remove_idle(victim)
                    # close() außerhalb des Locks (kill+wait darf acquire/release nicht blockieren);
                    # Slot bleibt belegt (total unverändert), wir spawnen ihn gleich neu.
                    asyncio.create_task(victim.close())
                    break
                await self.cond.wait()  # alles busy -> auf Freigabe warten

        # Spawn außerhalb des Locks (Init ~0.8s soll andere nicht blockieren).
        p = Proc(key, args)
        try:
            await p.start()
        except BaseException:
            async with self.cond:
                self.total -= 1
                self.cond.notify_all()
            raise
        return key, p, False

    def _pick_victim(self):
        best = None
        for lst in self.idle.values():
            for p in lst:
                if best is None or p.last_used < best.last_used:
                    best = p
        return best

    def _remove_idle(self, p):
        lst = self.idle.get(p.key)
        if lst and p in lst:
            lst.remove(p)

    async def release(self, key, p):
        async with self.cond:
            if self._closing or p.dead or p.uses >= settings.pool_max_uses:
                self.total -= 1
                asyncio.create_task(p.close())
            else:
                p.last_used = time.monotonic()
                self.idle[key].append(p)
            self.cond.notify_all()

    async def start_reaper(self):
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap())

    async def _reap(self):
        while True:
            await asyncio.sleep(settings.pool_reap_interval)
            now = time.monotonic()
            async with self.cond:
                for k, lst in list(self.idle.items()):
                    keep = []
                    for p in lst:
                        if now - p.last_used > settings.pool_idle_ttl:
                            self.total -= 1
                            asyncio.create_task(p.close())
                        else:
                            keep.append(p)
                    self.idle[k] = keep
                self.cond.notify_all()

    async def shutdown(self):
        self._closing = True
        if self._reaper:
            self._reaper.cancel()
        async with self.cond:
            procs = [p for lst in self.idle.values() for p in lst]
            self.idle.clear()
            self.total = 0
        for p in procs:
            await p.close()

    def snapshot(self):
        return {"live_procs": self.total,
                "idle": {k: len(v) for k, v in self.idle.items() if v},
                "max_procs": settings.pool_max_procs}


pool = Pool()


async def pooled_drive_turn(prompt, mcp_tools, model, stats, effort=None):
    # Bis zu 2 Versuche: eine idle gecrashte Instanz kann trotz Liveness-Check im Race
    # sterben. Retry ist nur sicher, SOLANGE noch nichts an den Client geflossen ist
    # (bei Streaming kann man Teil-Output nicht zurücknehmen).
    for attempt in (1, 2):
        t_acq = time.perf_counter()
        key, p, reused = await pool.acquire(model, mcp_tools, effort)
        stats["reused"] = reused
        if not reused:  # neue Instanz: Acquire = Spawn + Warmup-Init
            stats["spawn_ms"] = (time.perf_counter() - t_acq) * 1000
        agen = p.run_turn(prompt, stats)
        completed = False
        yielded_any = False
        retry = False
        try:
            while True:
                try:
                    ev = await agen.__anext__()
                except StopAsyncIteration:
                    completed = True
                    break
                # Toter Prozess VOR dem ersten Event -> transparent auf frische Instanz ausweichen.
                if (attempt == 1 and not yielded_any
                        and ev[0] == "error" and ev[1].get("type") == "cli_exit"):
                    retry = True
                    break
                yielded_any = True
                yield ev
        finally:
            with contextlib.suppress(Exception):
                await agen.aclose()
            if retry or not completed:  # tot/Client-Abbruch -> Zustand unklar -> verwerfen
                p.dead = True
            await pool.release(key, p)
        if not retry:
            return
        log.warning("pooled proc war tot vor erstem Event, retry auf frischer Instanz")
        stats.pop("outcome", None)  # sauberer Zustand für den zweiten Versuch
