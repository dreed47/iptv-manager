"""MPEG-TS stream analysis.

Observe-only: TsObserver parses a transport stream as it flows and measures what the
upstream actually does — where each upstream session's timeline starts relative to the
previous one (provider replay vs. reset vs. clean continuation), timestamp jumps within a
session, keyframe spacing, time-to-first-keyframe, codecs, and packet-level errors. It
never alters the bytes.

Single-packet PSI sections (PAT/PMT) are assumed, which holds for virtually all IPTV
feeds; a multi-packet PMT would simply leave codec info unknown.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

TS_PACKET = 188
SYNC = 0x47
NULL_PID = 0x1FFF
PTS_HZ = 90_000
PTS_WRAP = 1 << 33
JUMP_THRESHOLD = PTS_HZ          # >1s between consecutive frames counts as a jump
REWIND_WINDOW = 60 * PTS_HZ      # a backwards boundary within 60s looks like provider replay
GOP_SAMPLE_CAP = 1000

STREAM_TYPES = {
    0x01: "mpeg1video", 0x02: "mpeg2video", 0x1B: "h264", 0x24: "hevc",
    0x03: "mp1audio", 0x04: "mp2audio", 0x0F: "aac", 0x11: "aac_latm",
    0x81: "ac3", 0x87: "eac3", 0x06: "private",
}
VIDEO_TYPES = {0x01, 0x02, 0x1B, 0x24}


def pts_delta(a: int, b: int) -> int:
    """Signed b - a on the 33-bit wrapping 90kHz clock."""
    d = (b - a) % PTS_WRAP
    return d - PTS_WRAP if d >= PTS_WRAP // 2 else d


def _read_ts(buf, p: int) -> int:
    return (
        ((buf[p] >> 1) & 0x07) << 30
        | buf[p + 1] << 22
        | (buf[p + 2] >> 1) << 15
        | buf[p + 3] << 7
        | buf[p + 4] >> 1
    )


@dataclass
class _Session:
    index: int
    reason: str
    started_at: float = field(default_factory=time.time)
    boundary: str | None = None
    boundary_s: float | None = None
    bytes: int = 0
    packets: int = 0
    video_codec: str | None = None
    audio_codecs: list[str] = field(default_factory=list)
    pmt_changes: int = 0
    first_dts: int | None = None
    last_dts: int | None = None
    first_idr_dts: int | None = None
    first_idr_bytes: int | None = None
    gop_intervals: list[float] = field(default_factory=list)
    jumps_fwd: int = 0
    jumps_back: int = 0
    largest_jump_s: float = 0.0
    cc_errors: int = 0
    tei: int = 0
    resyncs: int = 0

    def summary(self) -> dict:
        wall = max(time.time() - self.started_at, 0.001)
        media = (
            pts_delta(self.first_dts, self.last_dts) / PTS_HZ
            if self.first_dts is not None and self.last_dts is not None else None
        )
        first_idr_s = (
            pts_delta(self.first_dts, self.first_idr_dts) / PTS_HZ
            if self.first_dts is not None and self.first_idr_dts is not None else None
        )
        gops = self.gop_intervals
        return {
            "session": self.index,
            "reason": self.reason,
            "boundary": self.boundary,
            "boundary_s": round(self.boundary_s, 2) if self.boundary_s is not None else None,
            "wall_s": round(wall, 1),
            "media_s": round(media, 1) if media is not None else None,
            "realtime_ratio": round(media / wall, 2) if media is not None and wall >= 5 else None,
            "bytes": self.bytes,
            "kbps": round(self.bytes * 8 / 1000 / wall),
            "video": self.video_codec,
            "audio": self.audio_codecs,
            "pmt_changes": self.pmt_changes,
            "first_idr_s": round(first_idr_s, 2) if first_idr_s is not None else None,
            "first_idr_bytes": self.first_idr_bytes,
            "gop_avg_s": round(sum(gops) / len(gops), 2) if gops else None,
            "gop_max_s": round(max(gops), 2) if gops else None,
            "jumps_fwd": self.jumps_fwd,
            "jumps_back": self.jumps_back,
            "largest_jump_s": round(self.largest_jump_s, 2),
            "cc_errors": self.cc_errors,
            "tei": self.tei,
            "resyncs": self.resyncs,
        }


class TsObserver:
    """Feed it the raw bytes of a stream; call new_session() at each upstream (re)connect."""

    def __init__(self, on_session_end: Callable[[dict], None] | None = None):
        self._on_session_end = on_session_end
        self._session: _Session | None = None
        self._session_count = 0
        self._prev_last_dts: int | None = None
        self._carry = b""
        self._cc: dict[int, int] = {}
        self.pmt_pids: set[int] = set()
        self.streams: dict[int, int] = {}
        self.video_pid: int | None = None
        self._scanning = False
        self._nal_tail = b""
        self._au_ts: int | None = None
        self._last_idr_ts: int | None = None

    # ---- session lifecycle -------------------------------------------------

    def new_session(self, reason: str = "") -> None:
        self.end_session()
        self._session_count += 1
        self._session = _Session(index=self._session_count, reason=reason)
        self._carry = b""
        self._cc.clear()
        self._scanning = False
        self._nal_tail = b""
        self._au_ts = None
        self._last_idr_ts = None

    def end_session(self) -> dict | None:
        s = self._session
        if s is None:
            return None
        self._session = None
        if s.last_dts is not None:
            self._prev_last_dts = s.last_dts
        if s.packets == 0:
            return None
        summary = s.summary()
        if self._on_session_end:
            self._on_session_end(summary)
        return summary

    def snapshot(self) -> dict | None:
        return self._session.summary() if self._session else None

    # ---- byte intake --------------------------------------------------------

    def feed(self, data: bytes) -> None:
        if self._session is None:
            self.new_session("implicit")
        s = self._session
        s.bytes += len(data)
        buf = self._carry + data if self._carry else data
        n = len(buf)
        i = 0
        while i + TS_PACKET <= n:
            if buf[i] != SYNC:
                s.resyncs += 1
                j = i + 1
                while True:
                    j = buf.find(b"\x47", j)
                    if j < 0 or j + TS_PACKET >= n or buf[j + TS_PACKET] == SYNC:
                        break
                    j += 1
                if j < 0:
                    i = n
                    break
                i = j
                continue
            self._packet(buf, i)
            i += TS_PACKET
        self._carry = bytes(buf[i:])

    # ---- packet parsing -----------------------------------------------------

    def _packet(self, buf, i: int) -> None:
        s = self._session
        s.packets += 1
        b1 = buf[i + 1]
        if b1 & 0x80:
            s.tei += 1
        pid = ((b1 & 0x1F) << 8) | buf[i + 2]
        if pid == NULL_PID:
            return
        b3 = buf[i + 3]
        afc = (b3 >> 4) & 0x3
        pos = i + 4
        end = i + TS_PACKET
        discontinuity = False
        if afc & 2:
            af_len = buf[pos]
            if af_len and pos + 1 < end:
                discontinuity = bool(buf[pos + 1] & 0x80)
            pos += 1 + af_len
        if not afc & 1:
            return
        cc = b3 & 0x0F
        prev = self._cc.get(pid)
        if prev is not None and not discontinuity and cc != prev and cc != (prev + 1) & 0x0F:
            s.cc_errors += 1
        self._cc[pid] = cc
        if pos >= end:
            return
        pusi = b1 & 0x40
        if pid == 0:
            if pusi:
                self._parse_pat(buf, pos, end)
        elif pid in self.pmt_pids:
            if pusi:
                self._parse_pmt(buf, pos, end)
        elif pid == self.video_pid:
            if pusi:
                self._video_pes_start(buf, pos, end)
            elif self._scanning:
                self._scan_nals(bytes(buf[pos:end]))

    def _parse_pat(self, buf, pos: int, end: int) -> None:
        p = pos + 1 + buf[pos]
        if p + 8 > end or buf[p] != 0x00:
            return
        section_end = min(p + 3 + (((buf[p + 1] & 0x0F) << 8) | buf[p + 2]) - 4, end)
        pmts = set()
        q = p + 8
        while q + 4 <= section_end:
            if (buf[q] << 8) | buf[q + 1]:
                pmts.add(((buf[q + 2] & 0x1F) << 8) | buf[q + 3])
            q += 4
        if pmts:
            self.pmt_pids = pmts

    def _parse_pmt(self, buf, pos: int, end: int) -> None:
        p = pos + 1 + buf[pos]
        if p + 12 > end or buf[p] != 0x02:
            return
        section_end = min(p + 3 + (((buf[p + 1] & 0x0F) << 8) | buf[p + 2]) - 4, end)
        q = p + 12 + (((buf[p + 10] & 0x0F) << 8) | buf[p + 11])
        streams: dict[int, int] = {}
        while q + 5 <= section_end:
            stream_type = buf[q]
            epid = ((buf[q + 1] & 0x1F) << 8) | buf[q + 2]
            es_len = ((buf[q + 3] & 0x0F) << 8) | buf[q + 4]
            if stream_type == 0x06:
                d, dend = q + 5, min(q + 5 + es_len, section_end)
                while d + 2 <= dend:
                    if buf[d] == 0x6A:
                        stream_type = 0x81
                    elif buf[d] == 0x7A:
                        stream_type = 0x87
                    d += 2 + buf[d + 1]
            streams[epid] = stream_type
            q += 5 + es_len
        if not streams:
            return
        s = self._session
        if streams != self.streams:
            if self.streams:
                s.pmt_changes += 1
            self.streams = streams
            self.video_pid = next((pid for pid, st in streams.items() if st in VIDEO_TYPES), None)
        elif s.video_codec or s.audio_codecs:
            return
        s.video_codec = STREAM_TYPES.get(streams.get(self.video_pid), None) if self.video_pid else None
        s.audio_codecs = [
            STREAM_TYPES.get(st, hex(st)) for pid, st in streams.items() if st not in VIDEO_TYPES
        ]

    def _video_pes_start(self, buf, pos: int, end: int) -> None:
        self._scanning = False
        if end - pos < 9 or buf[pos] or buf[pos + 1] or buf[pos + 2] != 1:
            return
        flags = buf[pos + 7] >> 6
        ts = None
        if flags == 3 and pos + 19 <= end:
            ts = _read_ts(buf, pos + 14)          # DTS: monotonic even with B-frames
        elif flags & 2 and pos + 14 <= end:
            ts = _read_ts(buf, pos + 9)
        if ts is not None:
            self._on_video_ts(ts)
        self._au_ts = ts
        self._scanning = True
        self._nal_tail = b""
        payload = pos + 9 + buf[pos + 8]
        if payload < end:
            self._scan_nals(bytes(buf[payload:end]))

    def _on_video_ts(self, t: int) -> None:
        s = self._session
        if s.first_dts is None:
            s.first_dts = t
            if self._prev_last_dts is None:
                s.boundary = "first"
            else:
                d = pts_delta(self._prev_last_dts, t)
                s.boundary_s = d / PTS_HZ
                if abs(d) <= JUMP_THRESHOLD:
                    s.boundary = "continuous"
                elif d > 0:
                    s.boundary = "forward"
                elif d >= -REWIND_WINDOW:
                    s.boundary = "rewind"
                else:
                    s.boundary = "reset"
        elif s.last_dts is not None:
            d = pts_delta(s.last_dts, t)
            if d > JUMP_THRESHOLD:
                s.jumps_fwd += 1
            elif d < -JUMP_THRESHOLD:
                s.jumps_back += 1
            if abs(d) > JUMP_THRESHOLD:
                s.largest_jump_s = max(s.largest_jump_s, abs(d) / PTS_HZ)
        s.last_dts = t

    def _scan_nals(self, data: bytes) -> None:
        buf = self._nal_tail + data
        codec = self.streams.get(self.video_pid)
        i = 0
        while True:
            j = buf.find(b"\x00\x00\x01", i)
            if j < 0 or j + 3 >= len(buf):
                break
            h = buf[j + 3]
            if codec == 0x24:                      # HEVC
                nal = (h >> 1) & 0x3F
                if 16 <= nal <= 21:
                    self._on_keyframe()
                    return
                if nal <= 9:
                    self._scanning = False
                    return
            elif codec in (0x01, 0x02):            # MPEG-1/2 video
                if h == 0x00 and j + 5 < len(buf):
                    if (buf[j + 5] >> 3) & 0x07 == 1:
                        self._on_keyframe()
                    else:
                        self._scanning = False
                    return
            else:                                  # H.264
                nal = h & 0x1F
                if nal == 5:
                    self._on_keyframe()
                    return
                if nal == 1:
                    self._scanning = False
                    return
            i = j + 3
        self._nal_tail = buf[-3:]

    def _on_keyframe(self) -> None:
        self._scanning = False
        s, t = self._session, self._au_ts
        if t is None:
            return
        if s.first_idr_dts is None:
            s.first_idr_dts = t
            s.first_idr_bytes = s.packets * TS_PACKET
        if self._last_idr_ts is not None:
            interval = pts_delta(self._last_idr_ts, t) / PTS_HZ
            if 0 < interval < 30 and len(s.gop_intervals) < GOP_SAMPLE_CAP:
                s.gop_intervals.append(interval)
        self._last_idr_ts = t
