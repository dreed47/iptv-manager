"""Timeline owner: stitches successive upstream sessions into one continuous MPEG-TS stream.

Input is FFmpeg's normalized output for each upstream session (fixed PIDs, so PAT/PMT are
identical from session to session). For every session after the first, all timestamps are
shifted so the session continues exactly where the previous one stopped:

- video/audio PTS and DTS (PES headers) and the PCR (adaptation field) get one per-session
  offset, which keeps A/V sync and PCR/PTS relationships intact;
- a session is only started on a video keyframe, optionally after skipping its first
  `skip_seconds` (content the provider replayed that viewers already saw);
- audio from the new session that would overlap audio already sent is dropped;
- continuity counters are rewritten per PID so they never jump at a session boundary.

The result: clients see one stream whose clock never jumps or rewinds, no matter how often
the upstream connection is replaced underneath it.
"""
from __future__ import annotations

from collections import deque

from streaming.tsfix import PTS_HZ, PTS_WRAP, TS_PACKET, _read_ts, pts_delta

VIDEO_PID = 0x100
AUDIO_PID = 0x101
ES_PIDS = (VIDEO_PID, AUDIO_PID)
_MAX_FRAME_TICKS = PTS_HZ // 5           # sanity cap for an inter-frame delta (200 ms)

_PENDING_AUDIO_PACKETS = 600             # ~4s of 192 kbit/s audio
_AUDIO_BACKFILL = PTS_HZ                 # at most 1s of audio from before a session's first keyframe

_DROP, _KEEP, _FRAME_START = 0, 1, 2     # per-packet verdicts


def _write_ts(buf: bytearray, p: int, value: int) -> None:
    value %= PTS_WRAP
    buf[p] = (buf[p] & 0xF0) | (((value >> 30) & 0x07) << 1) | 1
    buf[p + 1] = (value >> 22) & 0xFF
    buf[p + 2] = (((value >> 15) & 0x7F) << 1) | 1
    buf[p + 3] = (value >> 7) & 0xFF
    buf[p + 4] = ((value & 0x7F) << 1) | 1


def _shift_pcr(buf: bytearray, p: int, offset: int) -> None:
    base = (buf[p] << 25) | (buf[p + 1] << 17) | (buf[p + 2] << 9) | (buf[p + 3] << 1) | (buf[p + 4] >> 7)
    ext = ((buf[p + 4] & 0x01) << 8) | buf[p + 5]
    base = (base + offset) % PTS_WRAP
    buf[p] = (base >> 25) & 0xFF
    buf[p + 1] = (base >> 17) & 0xFF
    buf[p + 2] = (base >> 9) & 0xFF
    buf[p + 3] = (base >> 1) & 0xFF
    buf[p + 4] = ((base & 0x01) << 7) | 0x7E | ((ext >> 8) & 0x01)
    buf[p + 5] = ext & 0xFF


def _later(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return b if pts_delta(a, b) > 0 else a


class TimelineRewriter:
    def __init__(self):
        self._cc: dict[int, int] = {}
        self._carry = b""
        self._next_ts: int | None = None       # where the next session's first keyframe lands
        self._next_pts: int | None = None      # earliest display time for that keyframe
        self._audio_end: int | None = None     # end of the last audio frame sent
        self.sessions = 0
        self.skipped_seconds = 0.0
        self._start_session(0.0)

    # ---- session control ----------------------------------------------------

    def new_session(self, skip_seconds: float = 0.0) -> None:
        """Call before feeding the output of a new upstream session. The previous session's
        held-back final frame is discarded: the connection most likely died mid-frame."""
        self._finish_session()
        self._start_session(skip_seconds)

    def _start_session(self, skip_seconds: float) -> None:
        self.sessions += 1
        self._carry = b""
        self._held = bytearray()
        self._started = False
        self._offset = 0
        self._skip = int(max(0.0, skip_seconds) * PTS_HZ)
        self._ref: int | None = None             # first timestamp of the session, either stream
        self._dropping: dict[int, bool] = {}
        self._last_v: int | None = None
        self._v_frame = 3003
        self._last_a: int | None = None
        self._a_frame = 2880
        self._max_vpts: int | None = None
        self._sent: tuple[int | None, int | None] = (None, None)   # (_last_v, _max_vpts) as flushed
        # FFmpeg muxes audio up to ~0.5s ahead of the video it plays with, so audio that
        # belongs right after the first keyframe arrives before it. Keep recent audio packets
        # until the keyframe is found, then send the ones that play from the keyframe on.
        self._pending_audio: deque[bytearray] = deque(maxlen=_PENDING_AUDIO_PACKETS)
        self._replay: list[bytearray] = []
        self._audio_floor: int | None = None

    def _finish_session(self) -> None:
        if not self._started:
            return
        last_v, max_vpts = self._sent            # the held-back frame is never sent
        video_end = last_v + self._v_frame if last_v is not None else None
        audio_end = self._last_a + self._a_frame if self._last_a is not None else None
        # Video continues seamlessly; new audio that overlaps audio already sent is dropped.
        self._next_ts = video_end if video_end is not None else audio_end
        # display order too: a cut after a P-frame leaves its B-frames unsent, so the P-frame
        # shows later than its decode time suggests
        self._next_pts = max_vpts + self._v_frame if max_vpts is not None else None
        self._audio_end = _later(self._audio_end, audio_end)

    # ---- packet processing --------------------------------------------------

    def process(self, data: bytes) -> bytes:
        """Returns rewritten output. Video since the latest frame start is held back until the
        next frame starts, so a frame cut short by a dropped connection is never sent. Audio
        is never truncated (FFmpeg encodes it and writes whole frames) and goes straight out."""
        buf = bytearray(self._carry + data if self._carry else data)
        usable = len(buf) // TS_PACKET * TS_PACKET
        self._carry = bytes(buf[usable:])
        out = bytearray()
        for i in range(0, usable, TS_PACKET):
            if buf[i] != 0x47:
                continue
            if not self._started and buf[i + 1] & 0x1F == AUDIO_PID >> 8 and buf[i + 2] == AUDIO_PID & 0xFF:
                self._pending_audio.append(bytearray(buf[i:i + TS_PACKET]))
            verdict = self._packet(buf, i)
            if verdict == _FRAME_START:
                out += self._emit(self._held)
                self._held = bytearray()
            if self._replay:
                out += self._emit(self._replay_audio())
            if not verdict:
                continue
            pkt = buf[i:i + TS_PACKET]
            if pkt[1] & 0x1F == VIDEO_PID >> 8 and pkt[2] == VIDEO_PID & 0xFF:
                self._held += pkt
            else:
                out += self._emit(pkt)
        return bytes(out)

    def _emit(self, held: bytearray) -> bytearray:
        """Number continuity counters per PID at send time, so packets that are discarded
        (a session's held-back tail) never leave a gap."""
        for i in range(0, len(held), TS_PACKET):
            pid = ((held[i + 1] & 0x1F) << 8) | held[i + 2]
            last = self._cc.get(pid)
            if held[i + 3] & 0x10:                       # has payload
                cc = 0 if last is None else (last + 1) & 0x0F
            else:
                cc = last if last is not None else 0
            held[i + 3] = (held[i + 3] & 0xF0) | cc
            self._cc[pid] = cc
        return held

    def _replay_audio(self) -> bytearray:
        """Run the audio buffered before this session's first keyframe through the normal
        path; only frames playing at or after the keyframe survive."""
        out = bytearray()
        for pkt in self._replay:
            if self._packet(pkt, 0):
                out += pkt
        self._replay = []
        self._audio_floor = None
        return out

    def _packet(self, buf: bytearray, i: int) -> int:
        b1 = buf[i + 1]
        pid = ((b1 & 0x1F) << 8) | buf[i + 2]
        afc = (buf[i + 3] >> 4) & 0x3
        has_payload = bool(afc & 1)

        verdict = _KEEP
        if pid in ES_PIDS:
            pos = i + 4
            pcr_at = None
            if afc & 2:
                af_len = buf[pos]
                if af_len and buf[pos + 1] & 0x10:
                    pcr_at = pos + 2
                pos += 1 + af_len
            if b1 & 0x40 and has_payload:
                if not self._pes_start(buf, i, pid, pos):
                    self._dropping[pid] = True
                    return _DROP
                self._dropping[pid] = False
                if pid == VIDEO_PID:
                    verdict = _FRAME_START
            elif self._dropping.get(pid, True) and has_payload:
                return _DROP
            if not self._started:
                return _DROP
            if pcr_at is not None:
                _shift_pcr(buf, pcr_at, self._offset)
        return verdict

    def _pes_start(self, buf: bytearray, i: int, pid: int, pos: int) -> bool:
        """Decide whether this PES (and its continuation packets) is sent; shift its timestamps."""
        end = i + TS_PACKET
        if end - pos < 14 or buf[pos] or buf[pos + 1] or buf[pos + 2] != 1:
            return self._started
        flags = buf[pos + 7] >> 6
        if not flags & 2:
            return self._started
        has_dts = flags == 3 and pos + 19 <= end
        pts = _read_ts(buf, pos + 9)
        dts = _read_ts(buf, pos + 14) if has_dts else pts

        if not self._started:
            # skip is measured from the session's first timestamp: FFmpeg maps the start of
            # its input there, which is where the engine measured the replay from
            if self._ref is None:
                self._ref = dts
            if pid != VIDEO_PID:
                return False
            keyframe = bool(buf[i + 3] & 0x20) and buf[i + 4] and buf[i + 5] & 0x40
            if not keyframe or pts_delta(self._ref, dts) < self._skip:
                return False
            self._started = True
            self.skipped_seconds += pts_delta(self._ref, dts) / PTS_HZ if self._skip else 0.0
            self._offset = 0 if self._next_ts is None else pts_delta(dts, self._next_ts)
            if self._next_pts is not None:
                self._offset = max(self._offset, pts_delta(pts, self._next_pts))
            # buffered audio may fill back to where the previous session's audio ended (the
            # source's own muxing can leave audio trailing video when a connection is cut)
            kf = (dts + self._offset) % PTS_WRAP
            self._audio_floor = _later(self._audio_end, (kf - _AUDIO_BACKFILL) % PTS_WRAP) \
                if self._audio_end is not None else kf
            self._replay = list(self._pending_audio)
            self._pending_audio.clear()

        new_pts = (pts + self._offset) % PTS_WRAP
        new_dts = (dts + self._offset) % PTS_WRAP
        if pid == AUDIO_PID:
            if self._audio_end is not None and pts_delta(self._audio_end, new_pts) < 0:
                return False                   # would overlap audio the client already has
            if self._audio_floor is not None and pts_delta(self._audio_floor, new_pts) < 0:
                return False                   # buffered audio from before the keyframe
        _write_ts(buf, pos + 9, new_pts)
        if has_dts:
            _write_ts(buf, pos + 14, new_dts)

        if pid == VIDEO_PID:
            self._sent = (self._last_v, self._max_vpts)   # every video frame before this one is flushed now
            self._max_vpts = _later(self._max_vpts, new_pts)
            if self._last_v is not None:
                d = pts_delta(self._last_v, new_dts)
                if 0 < d <= _MAX_FRAME_TICKS:
                    self._v_frame = d
            self._last_v = _later(self._last_v, new_dts)
        else:
            if self._last_a is not None:
                d = pts_delta(self._last_a, new_pts)
                if 0 < d <= _MAX_FRAME_TICKS:
                    self._a_frame = d
            self._last_a = _later(self._last_a, new_pts)
        return True
