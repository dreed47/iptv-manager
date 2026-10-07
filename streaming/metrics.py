"""Per-channel stream observations.

Each upstream connection's bytes are fed to a TsObserver. Observers are kept per channel
for a short while after a stream ends, so a client that reconnects to the same channel
continues the same observer and the boundary between the two upstream sessions (clean
continuation, provider replay, timeline reset) gets measured too.

Every finished upstream session is logged and appended as one JSON line to
config.STREAM_OBSERVE_LOG, which survives container log rotation.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque

import config
from streaming.tsfix import TsObserver

logger = logging.getLogger(__name__)

_STREAM_ID_RE = re.compile(r"/(\d+)(?:\.\w+)?(?:\?.*)?$")
_LINGER_SECS = 120
_LOG_MAX_BYTES = 20 * 1024 * 1024

_lock = threading.Lock()
_idle: dict[tuple, "ChannelObservation"] = {}
_recent: deque = deque(maxlen=500)
_write_lock = threading.Lock()


def channel_key(item_id, url: str) -> tuple:
    """Identify a channel by provider + upstream stream id, so token/domain rotation in the
    URL doesn't make the same channel look like a different one."""
    m = _STREAM_ID_RE.search(url or "")
    return (item_id, m.group(1) if m else url)


class ChannelObservation:
    def __init__(self, key: tuple, label: str, source: str):
        self.key = key
        self.label = label
        self.source = source
        self.released_at = 0.0
        self._failed = False
        self._observer = TsObserver(on_session_end=self._record)

    def new_session(self, reason: str) -> None:
        self._guard(self._observer.new_session, reason)

    def feed(self, data: bytes) -> None:
        self._guard(self._observer.feed, data)

    def snapshot(self) -> dict | None:
        return None if self._failed else self._observer.snapshot()

    @property
    def current(self):
        """Live counters of the current upstream session (read-only), or None."""
        return None if self._failed else self._observer.current

    def _guard(self, fn, arg) -> None:
        if self._failed:
            return
        try:
            fn(arg)
        except Exception:
            # Observation must never affect streaming; stop observing this channel instead.
            self._failed = True
            logger.exception(f"Stream observer disabled for {self.label} after an error")

    def _record(self, summary: dict) -> None:
        record = {
            "ts": round(time.time()),
            "source": self.source,
            "item_id": self.key[0],
            "stream_id": self.key[1],
            "channel": self.label,
            **summary,
        }
        _recent.append(record)
        logger.info(_format(record))
        _append_jsonl(record)


def acquire(item_id, url: str, label: str, source: str) -> ChannelObservation:
    key = channel_key(item_id, url)
    with _lock:
        now = time.time()
        for k in [k for k, o in _idle.items() if now - o.released_at > _LINGER_SECS]:
            del _idle[k]
        obs = _idle.pop((source, key), None)
    if obs is None:
        obs = ChannelObservation(key, label, source)
    obs.label = label
    return obs


def release(obs: ChannelObservation) -> None:
    obs._guard(lambda _: obs._observer.end_session(), None)
    obs.released_at = time.time()
    with _lock:
        _idle[(obs.source, obs.key)] = obs


def recent(limit: int = 100) -> list[dict]:
    return list(_recent)[-limit:]


def _append_jsonl(record: dict) -> None:
    path = config.STREAM_OBSERVE_LOG
    if not path:
        return
    try:
        with _write_lock:
            if os.path.exists(path) and os.path.getsize(path) > _LOG_MAX_BYTES:
                os.replace(path, path + ".1")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning(f"Could not write stream observation: {exc}")


def _format(r: dict) -> str:
    boundary = r["boundary"] or "?"
    if r["boundary_s"] is not None and boundary not in ("first", "continuous"):
        boundary += f" {r['boundary_s']:+.1f}s"
    gop = f"GOP {r['gop_avg_s']}s/max {r['gop_max_s']}s" if r["gop_avg_s"] else "GOP ?"
    first_idr = f"first IDR +{r['first_idr_s']}s" if r["first_idr_s"] is not None else "no IDR seen"
    rt = f" rt {r['realtime_ratio']}x" if r["realtime_ratio"] is not None else ""
    return (
        f"Stream obs [{r['source']} {r['channel']}] session {r['session']} ({r['reason']}): "
        f"boundary={boundary}, {r['wall_s']}s @{r['kbps']}kbps{rt}, "
        f"{r['video'] or '?'}/{'+'.join(r['audio']) or '?'}, {first_idr}, {gop}, "
        f"jumps fwd {r['jumps_fwd']}/back {r['jumps_back']}, "
        f"cc_err {r['cc_errors']}, tei {r['tei']}, resync {r['resyncs']}, pmt_chg {r['pmt_changes']}"
    )
