"""One ChannelEngine per active channel: a single upstream connection, normalized through
FFmpeg, split at keyframes into a RingBuffer that every consumer of the channel reads from.

Pipeline (three threads per engine):
    upstream reader  ──raw TS──▶  FFmpeg stdin
    FFmpeg stdout    ──clean TS──▶ TsSplitter ──▶ RingBuffer ──▶ consumers (asyncio, output_ts)
    FFmpeg stderr    ──▶ debug log / tail kept for failure reports

FFmpeg remuxes video (no re-encode), drops corrupt packets, normalizes audio, and always
emits the same PIDs, so its output is a predictable, well-formed stream. It flags every
keyframe with the TS random_access_indicator, which is what TsSplitter keys on.

Reconnects are invisible to viewers. The engine watches the raw upstream and replaces the
connection when it closes, errors, goes silent (ENGINE_STALL_SECS), runs slower than
ENGINE_MIN_SPEED × real-time, or rewinds. Every session's FFmpeg output goes through a
TimelineRewriter, which continues the output clock exactly where the previous session
stopped, starts the new session on a keyframe and trims content the provider replayed, so
the stream clients receive never jumps, rewinds or restarts. While reconnecting, viewers
get keepalive filler (output_ts). The engine gives up only after ENGINE_OUTAGE_SECS with
no output (or ENGINE_START_RETRIES failed connects before the first keyframe).
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

import requests

import config
from streaming import metrics as stream_metrics
from streaming.ringbuffer import RingBuffer
from streaming.timeline import TimelineRewriter
from streaming.tsfix import PTS_HZ, TsObserver, pts_delta

logger = logging.getLogger(__name__)

TS_PACKET = 188
VIDEO_PID = 0x100           # fixed by -mpegts_start_pid
PMT_PID = 0x1000            # fixed by -mpegts_pmt_start_pid
_NO_VIDEO_FALLBACK_BYTES = 4 * 1024 * 1024
_READ_SIZE = TS_PACKET * 348   # ~64KB
_MAX_REPLAY_SKIP = 60 * PTS_HZ   # a reconnect landing further back than this is a different timeline
_SKIP_WAIT_SECS = 10
_BACKOFF = (0.5, 1, 2, 4, 5)


def ffmpeg_command() -> list[str]:
    audio = config.ENGINE_AUDIO_CODEC
    if audio == "copy":
        audio_args = ["-c:a", "copy"]
    else:
        audio_args = ["-c:a", audio, "-ar", "48000", "-ac", "2", "-b:a", "192k"]
    return [
        "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning",
        "-fflags", "+genpts+discardcorrupt",
        "-f", "mpegts", "-i", "pipe:0",
        "-map", "0:v:0?", "-map", "0:a:0?",
        "-c:v", "copy", *audio_args,
        "-f", "mpegts",
        "-mpegts_service_id", "1",
        "-mpegts_pmt_start_pid", str(PMT_PID),
        "-mpegts_start_pid", str(VIDEO_PID),
        "pipe:1",
    ]


class TsSplitter:
    """Cuts FFmpeg's output at keyframes and remembers the latest PAT/PMT."""

    def __init__(self):
        self._carry = b""
        self._pat = b""
        self._pmt = b""
        self._out_bytes = 0
        self._seen_gop = False

    def _header(self) -> bytes:
        return self._pat + self._pmt if self._pat and self._pmt else b""

    def split(self, data: bytes) -> list[tuple[bytes, bytes | None]]:
        """Returns (segment, header) pairs. header is None for a continuation segment; for a
        segment that starts at a keyframe it is the PAT+PMT that preceded that keyframe in the
        stream — exactly what a decoder joining there needs, with continuity counters that
        flow straight into the next PAT/PMT the stream carries."""
        buf = self._carry + data if self._carry else data
        n = len(buf)
        i = 0
        while i < n and buf[i] != 0x47:          # FFmpeg output is aligned; this only guards a bad start
            i += 1
        start = i
        usable = start + (n - start) // TS_PACKET * TS_PACKET
        cuts: dict[int, bytes] = {}
        for p in range(start, usable, TS_PACKET):
            b1 = buf[p + 1]
            if not b1 & 0x40:
                continue
            pid = ((b1 & 0x1F) << 8) | buf[p + 2]
            if pid == 0:
                self._pat = bytes(buf[p:p + TS_PACKET])
            elif pid == PMT_PID:
                self._pmt = bytes(buf[p:p + TS_PACKET])
            elif pid == VIDEO_PID and (buf[p + 3] >> 4) & 2 and buf[p + 4] and buf[p + 5] & 0x40:
                cuts[p] = self._header()
        self._carry = bytes(buf[usable:])
        self._out_bytes += usable - start

        if not cuts and not self._seen_gop and self._out_bytes >= _NO_VIDEO_FALLBACK_BYTES:
            cuts[start] = self._header()          # audio-only stream: make it joinable anyway
        if cuts:
            self._seen_gop = True
        bounds = [start, *[c for c in cuts if c > start], usable]
        return [(bytes(buf[a:b]), cuts.get(a)) for a, b in zip(bounds, bounds[1:]) if b > a]


@dataclass
class Consumer:
    client_ip: str
    user_agent: str
    source: str
    channel: str
    channel_name: str
    item_id: int
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: float = field(default_factory=time.time)
    last_chunk_at: float = field(default_factory=time.time)
    bytes_sent: int = 0
    killed: bool = False

    def as_session(self) -> dict:
        return {
            "session_id": self.id,
            "channel": self.channel,
            "channel_name": self.channel_name,
            "item_id": self.item_id,
            "client_ip": self.client_ip,
            "user_agent": self.user_agent,
            "started_at": self.started_at,
            "last_chunk_at": self.last_chunk_at,
            "bytes_sent": self.bytes_sent,
            "killed": self.killed,
        }


class ChannelEngine:
    def __init__(self, item_id: int, url: str, label: str):
        self.key = stream_metrics.channel_key(item_id, url)
        self.item_id = item_id
        self.url = url
        self.label = label
        self.ring = RingBuffer(config.ENGINE_RING_MB * 1024 * 1024)
        self.consumers: dict[str, Consumer] = {}
        self.lock = threading.Lock()
        self.started_at = time.time()
        self.idle_since = time.time()
        self.finished = False
        self.failure: str | None = None
        self.on_finished = None
        self._stop = threading.Event()
        self._stop_reason = ""
        self._http = requests.Session()
        self._resp = None
        self._proc: subprocess.Popen | None = None
        self._end_reason = ""
        self._obs = None
        self._monitor = None
        self._session_skip = 0.0
        self._sessions_run = 0
        self._slow_streak = 0
        self._last_output = time.monotonic()
        self.reconnects = 0
        self.timeline = TimelineRewriter()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"engine-{self.key[1]}")

    # ---- control ----------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def stop(self, reason: str) -> None:
        if self._stop.is_set():
            return
        self._stop_reason = reason
        self._stop.set()
        for close in (lambda: self._resp and self._resp.close(), self._http.close,
                      lambda: self._proc and self._proc.kill()):
            try:
                close()
            except Exception:
                pass

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def attach(self, consumer: Consumer) -> None:
        with self.lock:
            self.consumers[consumer.id] = consumer

    def detach(self, consumer: Consumer) -> int:
        with self.lock:
            self.consumers.pop(consumer.id, None)
            if not self.consumers:
                self.idle_since = time.time()
            return len(self.consumers)

    def client_ips(self) -> set[str]:
        with self.lock:
            return {c.client_ip for c in self.consumers.values()}

    # ---- pipeline -------------------------------------------------------------------

    def _run(self) -> None:
        self._obs = stream_metrics.acquire(self.item_id, self.url, self.label, "engine") if config.STREAM_OBSERVE else None
        self._monitor = self._obs or TsObserver()   # raw-upstream counters drive reconnect decisions
        rewriter = self.timeline
        splitter = TsSplitter()
        failures = 0
        reason = "initial"
        prev_max: int | None = None
        try:
            while not self._stop.is_set():
                if failures:
                    if not self.ring.joinable and failures > config.ENGINE_START_RETRIES:
                        break
                    if self.ring.joinable and time.monotonic() - self._last_output > config.ENGINE_OUTAGE_SECS:
                        self.failure = f"no data for {config.ENGINE_OUTAGE_SECS:.0f}s ({self.failure})"
                        break
                    if self._stop.wait(_BACKOFF[min(failures - 1, len(_BACKOFF) - 1)]):
                        break
                try:
                    self._resp = self._http.get(
                        self.url,
                        headers={"User-Agent": config.PROXY_USER_AGENT, "Accept": "*/*"},
                        stream=True,
                        timeout=(config.STREAM_CONNECT_TIMEOUT, config.ENGINE_STALL_SECS),
                    )
                    self._resp.raise_for_status()
                except Exception as exc:
                    self.failure = f"upstream connect failed: {exc}"
                    logger.warning(f"Engine [{self.label}] {self.failure} (attempt {failures + 1})")
                    failures += 1
                    continue
                self._monitor.new_session(reason)
                produced = self._run_session(rewriter, splitter, prev_max)
                cur = self._monitor.current
                if cur is not None and cur.max_dts is not None:
                    prev_max = cur.max_dts
                if self._stop.is_set():
                    break
                if produced:
                    failures = 0
                else:
                    failures += 1
                    self.failure = self._end_reason or "no video from upstream"
                if self.ring.joinable:
                    self.reconnects += 1
                    reason = f"reconnect: {self._end_reason}"
                    logger.info(f"Engine [{self.label}] {self._end_reason}; reconnecting (#{self.reconnects})")
                else:
                    reason = "retry"
        except Exception:
            logger.exception(f"Engine [{self.label}] crashed")
        finally:
            self.ring.close()
            self.finished = True
            self._http.close()
            if self._obs:
                stream_metrics.release(self._obs)
            reason = self._stop_reason or self.failure or self._end_reason or "ended"
            logger.info(f"Engine stopped [{self.label}]: {reason}, up {time.time() - self.started_at:.0f}s, "
                        f"{self.reconnects} reconnects, {rewriter.skipped_seconds:.1f}s replay trimmed")
            if self.on_finished:
                self.on_finished(self)

    def _run_session(self, rewriter: TimelineRewriter, splitter: TsSplitter, prev_max: int | None) -> bool:
        """Run one upstream connection through FFmpeg and the timeline rewriter into the ring.
        Returns True if this session produced output."""
        self._end_reason = ""
        self._session_skip = 0.0
        skip_ready = threading.Event()
        if prev_max is None:
            skip_ready.set()
        proc = subprocess.Popen(
            ffmpeg_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )
        self._proc = proc
        if self._stop.is_set():
            proc.kill()
        stderr_tail: deque[str] = deque(maxlen=20)
        threading.Thread(target=self._drain_stderr, args=(proc, stderr_tail), daemon=True,
                         name=f"engine-{self.key[1]}-stderr").start()
        writer = threading.Thread(target=self._feed_upstream, args=(proc, prev_max, skip_ready), daemon=True,
                                  name=f"engine-{self.key[1]}-upstream")
        writer.start()
        self._sessions_run += 1
        begun = self._sessions_run == 1          # the rewriter starts out in its first session
        logger.info(f"Engine session {self._sessions_run} [{self.label}] → {self.url}")

        produced = False
        fd = proc.stdout.fileno()
        while True:
            data = os.read(fd, _READ_SIZE)
            if not data:
                break
            if not begun:
                begun = True
                skip_ready.wait(_SKIP_WAIT_SECS)
                rewriter.new_session(self._session_skip)
                if self._session_skip:
                    logger.info(f"Engine [{self.label}] trimming {self._session_skip:.1f}s replayed by upstream")
            out = rewriter.process(data)
            if out:
                produced = True
                self._last_output = time.monotonic()
                for segment, header in splitter.split(out):
                    self.ring.append(segment, header)

        writer.join(timeout=5)
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
        proc.stdout.close()
        if rc not in (0, None) and not self._stop.is_set():
            self._end_reason = (self._end_reason or "ffmpeg exited") + f" (rc={rc}: {' | '.join(stderr_tail)[-300:]})"
        return produced

    def _feed_upstream(self, proc: subprocess.Popen, prev_max: int | None, skip_ready: threading.Event) -> None:
        resp = self._resp
        monitor = self._monitor
        started = time.monotonic()
        grace = min(config.ENGINE_SPEED_GRACE * (2 ** self._slow_streak), 60)
        samples: deque[tuple[float, int]] = deque()
        fed = 0
        try:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if self._stop.is_set():
                    break
                if not chunk:
                    continue
                monitor.feed(chunk)
                fed += len(chunk)
                cur = monitor.current
                if not skip_ready.is_set():
                    if cur is not None and cur.first_dts is not None:
                        d = pts_delta(cur.first_dts, prev_max)
                        self._session_skip = d / PTS_HZ if 0 < d <= _MAX_REPLAY_SKIP else 0.0
                        skip_ready.set()
                    elif fed > _NO_VIDEO_FALLBACK_BYTES:
                        skip_ready.set()
                if cur is not None and cur.jumps_back:
                    self._end_reason = "upstream rewound"
                    return
                now = time.monotonic()
                if now - started > 60:
                    self._slow_streak = 0
                if config.ENGINE_MIN_SPEED and cur is not None and cur.max_dts is not None and now - started >= grace:
                    samples.append((now, cur.max_dts))
                    while len(samples) > 1 and samples[1][0] <= now - config.ENGINE_SPEED_WINDOW:
                        samples.popleft()
                    span = now - samples[0][0]
                    if span >= config.ENGINE_SPEED_WINDOW:
                        speed = pts_delta(samples[0][1], cur.max_dts) / PTS_HZ / span
                        if speed < config.ENGINE_MIN_SPEED:
                            self._slow_streak += 1
                            self._end_reason = f"upstream slow ({speed:.2f}x real-time)"
                            return
                proc.stdin.write(chunk)
            self._end_reason = "upstream closed"
        except requests.exceptions.RequestException as exc:   # subclasses OSError: must come first
            self._end_reason = f"upstream error: {exc.__class__.__name__}"
        except (BrokenPipeError, ValueError, OSError) as exc:
            self._end_reason = f"ffmpeg input closed ({exc.__class__.__name__})"
        except Exception as exc:
            self._end_reason = f"upstream error: {exc}"
        finally:
            skip_ready.set()
            for close in (proc.stdin.close, resp.close):
                try:
                    close()
                except Exception:
                    pass

    def _drain_stderr(self, proc: subprocess.Popen, tail: deque) -> None:
        try:
            for raw in proc.stderr:
                line = raw.decode(errors="replace").strip()
                if line:
                    tail.append(line)
                    logger.debug(f"ffmpeg [{self.label}] {line}")
        finally:
            proc.stderr.close()
