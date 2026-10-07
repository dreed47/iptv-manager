"""Keyframe-indexed ring of MPEG-TS data shared by every consumer of one channel.

The producer (a thread) appends packet-aligned chunks; a chunk that begins a GOP (a video
keyframe) carries the PAT+PMT that preceded it. Consumers (asyncio tasks) hold a cursor:
they join at the most recent GOP start, and one that falls behind the oldest retained data
is moved forward to the latest GOP start rather than being handed a gap mid-GOP. Either
way the consumer is given that GOP's header to send first.
"""
from __future__ import annotations

import asyncio
import threading
from collections import deque

EOS = "eos"
SKIPPED = "skipped"
OK = "ok"


class RingBuffer:
    def __init__(self, max_bytes: int, max_read_bytes: int = 1024 * 1024):
        self._lock = threading.Lock()
        self._entries: deque[tuple[int, bytes, bytes | None]] = deque()
        self._next_seq = 0
        self._bytes = 0
        self._max_bytes = max_bytes
        self._max_read = max_read_bytes
        self._last_gop: tuple[int, bytes] | None = None
        self._eos = False
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = []

    # ---- producer side (thread) ---------------------------------------------

    def append(self, data: bytes, header: bytes | None) -> None:
        """header is None for a continuation chunk, else the PAT+PMT for a GOP-start chunk."""
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            self._entries.append((seq, data, header))
            self._bytes += len(data)
            if header is not None:
                self._last_gop = (seq, header)
            while self._bytes > self._max_bytes and len(self._entries) > 1:
                _, old, _ = self._entries.popleft()
                self._bytes -= len(old)
            self._wake()

    def close(self) -> None:
        with self._lock:
            self._eos = True
            self._wake()

    def _wake(self) -> None:
        for loop, fut in self._waiters:
            loop.call_soon_threadsafe(_resolve, fut)
        self._waiters.clear()

    # ---- consumer side (asyncio) ----------------------------------------------

    @property
    def joinable(self) -> bool:
        return self._last_gop is not None

    def join(self) -> tuple[int, bytes] | None:
        """(cursor, header) for a new consumer at the most recent GOP start, or None."""
        with self._lock:
            return self._last_gop

    def read(self, cursor: int) -> tuple[bytes, int, str]:
        """Non-blocking. Returns (data, new_cursor, status).

        If the cursor had fallen off the tail, status is SKIPPED and data begins with the
        header of the GOP it was moved to. EOS once the producer has finished and everything
        has been read; otherwise OK (data may be empty: wait and retry).
        """
        with self._lock:
            status = OK
            prefix = b""
            oldest = self._entries[0][0] if self._entries else self._next_seq
            if cursor < oldest:
                status = SKIPPED
                if self._last_gop and self._last_gop[0] >= oldest:
                    cursor, prefix = self._last_gop
                else:
                    cursor = oldest
            parts, size = [prefix] if prefix else [], 0
            for seq, data, _ in self._entries:
                if seq < cursor:
                    continue
                if size and size + len(data) > self._max_read:
                    break
                parts.append(data)
                size += len(data)
                cursor = seq + 1
            if not size and self._eos and status == OK:
                status = EOS
            return b"".join(parts), cursor, status

    async def wait(self, cursor: int, timeout: float) -> None:
        """Wait until there is data at/after cursor, the ring closes, or timeout."""
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._eos or cursor < self._next_seq:
                return
            fut = loop.create_future()
            self._waiters.append((loop, fut))
        try:
            await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            with self._lock:
                if (loop, fut) in self._waiters:
                    self._waiters.remove((loop, fut))


def _resolve(fut: asyncio.Future) -> None:
    if not fut.done():
        fut.set_result(None)
