import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import config
from streaming.engine import ffmpeg_command
from streaming.timeline import TimelineRewriter
from streaming.tsfix import TS_PACKET, TsObserver
from tests.test_tsfix import FFMPEG, _make_ts


def _normalize(raw: bytes) -> bytes:
    """Run raw provider bytes through the engine's FFmpeg normalize step, like one upstream session."""
    with mock.patch.object(config, "ENGINE_AUDIO_CODEC", "ac3"):
        cmd = ffmpeg_command()
    return subprocess.run(cmd, input=raw, capture_output=True, check=True).stdout


def _packets(path: str) -> dict[str, list[float]]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_packets", "-show_entries", "packet=codec_type,dts_time",
         "-of", "json", path], capture_output=True, text=True, check=True).stdout
    by_type: dict[str, list[float]] = {}
    for p in json.loads(out)["packets"]:
        if p.get("dts_time") not in (None, "N/A"):
            by_type.setdefault(p["codec_type"], []).append(float(p["dts_time"]))
    return by_type


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class TimelineRewriterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        raw = _make_ts(os.path.join(cls.tmp, "src.ts"), seconds=30)
        cut = lambda frac: int(len(raw) * frac) // TS_PACKET * TS_PACKET
        # Two upstream sessions, like a provider reconnect: A dies mid-frame around 10.5s;
        # B starts mid-GOP around 7.5s, replaying ~3s the viewer already saw.
        cls.session_a = _normalize(raw[:cut(0.35) + 100])
        cls.session_b = _normalize(raw[cut(0.25):])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _stitch(self, *sessions, skips=None):
        rw = TimelineRewriter()
        out = bytearray()
        for n, s in enumerate(sessions):
            if n:
                rw.new_session((skips or {}).get(n, 0.0))
            for i in range(0, len(s), 65536):           # arbitrary chunking, like the pump
                out += rw.process(s[i:i + 65536])
        return bytes(out), rw

    def _check_continuous(self, data: bytes) -> dict:
        path = os.path.join(self.tmp, "stitched.ts")
        with open(path, "wb") as f:
            f.write(data)
        decode_errors = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-f", "null", "-"],
                                       capture_output=True, text=True).stderr.strip()
        self.assertEqual(decode_errors, "", "stitched stream must decode cleanly")
        for kind, dts in _packets(path).items():
            gaps = [b - a for a, b in zip(dts, dts[1:])]
            self.assertTrue(all(g > 0 for g in gaps), f"{kind} DTS must strictly increase")
            self.assertLess(max(gaps), 0.25, f"{kind} timeline must not have holes")
        obs = TsObserver()
        obs.feed(data)
        s = obs.end_session()
        self.assertEqual((s["jumps_fwd"], s["jumps_back"], s["cc_errors"]), (0, 0, 0))
        return s

    def test_first_session_passes_through(self):
        out, _ = self._stitch(self.session_a)
        self.assertEqual(TsObserver().feed(out) or None, None)
        self._check_continuous(out)

    def test_two_sessions_become_one_continuous_stream(self):
        out, rw = self._stitch(self.session_a, self.session_b)
        s = self._check_continuous(out)
        self.assertEqual(rw.sessions, 2)
        self.assertEqual(s["video"], "h264")
        self.assertEqual(s["audio"], ["ac3"])

    def test_many_reconnects(self):
        out, rw = self._stitch(self.session_a, self.session_b, self.session_a, self.session_b)
        self._check_continuous(out)
        self.assertEqual(rw.sessions, 4)

    def test_session_that_dies_right_after_its_keyframe(self):
        # cut session B a few packets after its first keyframe starts: nothing of it is sent
        b = self.session_b
        first_kf = next(i for i in range(0, len(b), TS_PACKET)
                        if ((b[i + 1] & 0x1F) << 8 | b[i + 2]) == 0x100 and b[i + 3] & 0x20
                        and b[i + 4] and b[i + 5] & 0x40)
        out, rw = self._stitch(self.session_a, b[:first_kf + 20 * TS_PACKET], self.session_b)
        self._check_continuous(out)
        self.assertEqual(rw.sessions, 3)

    def test_replay_is_skipped(self):
        plain, _ = self._stitch(self.session_a, self.session_b)
        trimmed, rw = self._stitch(self.session_a, self.session_b, skips={1: 3.0})
        self._check_continuous(trimmed)
        media = lambda d: (lambda o: (o.feed(d), o.end_session())[1]["media_s"])(TsObserver())
        self.assertAlmostEqual(media(plain) - media(trimmed), 3.0, delta=2.1)   # within one GOP
        self.assertGreaterEqual(rw.skipped_seconds, 3.0)


if __name__ == "__main__":
    unittest.main()
