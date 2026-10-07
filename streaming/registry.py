"""Owns the live ChannelEngines: one per (provider, upstream stream id).

Provider connection budget: Item.max_sessions is the number of distinct upstream channels
a provider account may have open at once. Every viewer of a channel shares its engine, so
viewers beyond the first cost nothing. When a new channel would exceed the budget, idle
engines (no viewers, still lingering) are stopped first, then engines whose only viewers
are the requesting client itself (that client is switching channels).
"""
from __future__ import annotations

import logging
import threading

import config
from streaming import metrics as stream_metrics
from streaming.engine import ChannelEngine, Consumer

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_engines: dict[tuple, ChannelEngine] = {}


class BudgetExceeded(Exception):
    def __init__(self, active: int, limit: int):
        super().__init__(f"{active}/{limit} provider connections in use")
        self.active = active
        self.limit = limit


def acquire(item_id: int, url: str, label: str, max_sessions: int, consumer: Consumer,
            name: str = "") -> ChannelEngine:
    """Return the channel's engine (starting one if needed) with consumer already attached.
    Attaching under the registry lock means an idle engine can't be stopped in between."""
    key = stream_metrics.channel_key(item_id, url)
    victims: list[ChannelEngine] = []
    with _lock:
        engine = _engines.get(key)
        if engine and not engine.finished and not engine.stopping:
            engine.attach(consumer)
            return engine
        live = [e for e in _engines.values() if e.item_id == item_id and not e.finished and not e.stopping]
        need = len(live) + 1 - max(1, max_sessions)
        if need > 0:
            idle = sorted((e for e in live if not e.consumers), key=lambda e: e.idle_since)
            switching = [e for e in live if e.consumers and e.client_ips() <= {consumer.client_ip}]
            victims = (idle + switching)[:need]
            if len(victims) < need:
                raise BudgetExceeded(len(live), max_sessions)
            for v in victims:
                _engines.pop(v.key, None)
        engine = ChannelEngine(item_id, url, label, name)
        engine.on_finished = _forget
        engine.attach(consumer)
        _engines[key] = engine
    for v in victims:
        logger.info(f"Engine preempted [{v.label}] for [{label}] (provider {item_id} at {max_sessions} connection(s))")
        v.stop("preempted")
    engine.start()
    return engine


def release(engine: ChannelEngine, consumer: Consumer) -> None:
    remaining = engine.detach(consumer)
    if remaining:
        return
    if consumer.killed:
        engine.stop("killed by admin")
        return
    timer = threading.Timer(config.ENGINE_IDLE_SECS, _stop_if_idle, args=(engine,))
    timer.daemon = True
    timer.start()


def _stop_if_idle(engine: ChannelEngine) -> None:
    with _lock:
        if engine.consumers or engine.finished:
            return
        if _engines.get(engine.key) is engine:
            del _engines[engine.key]
    engine.stop(f"idle {config.ENGINE_IDLE_SECS}s")


def _forget(engine: ChannelEngine) -> None:
    with _lock:
        if _engines.get(engine.key) is engine:
            del _engines[engine.key]


def active_sessions() -> list[dict]:
    with _lock:
        engines = list(_engines.values())
    sessions = []
    for e in engines:
        feed = e.feed.name if e.feed is not e._primary else ""
        with e.lock:
            sessions.extend({**c.as_session(), "feed": feed} for c in e.consumers.values())
    return sessions


def kill(session_id: str) -> str | None:
    """Mark a viewer killed; its stream ends within about a second. Returns its client IP."""
    with _lock:
        engines = list(_engines.values())
    for e in engines:
        with e.lock:
            c = e.consumers.get(session_id)
        if c:
            c.killed = True
            return c.client_ip
    return None


def stop_all() -> None:
    with _lock:
        engines = list(_engines.values())
        _engines.clear()
    for e in engines:
        e.stop("shutdown")
