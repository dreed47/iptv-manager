import os
import shutil
import subprocess
import tempfile
import unittest

from streaming.tsfix import PTS_WRAP, TS_PACKET, TsObserver, pts_delta

FFMPEG = shutil.which("ffmpeg")


def _make_ts(path, *, seconds=10, offset=None, vcodec="libx264", gop_frames=60):
    cmd = [
        FFMPEG, "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
        "-t", str(seconds),
        "-c:v", vcodec, "-g", str(gop_frames), "-keyint_min", str(gop_frames),
        "-c:a", "aac",
    ]
    if vcodec == "libx264":
        cmd += ["-sc_threshold", "0", "-bf", "2", "-preset", "ultrafast"]
    elif vcodec == "libx265":
        cmd += ["-x265-params", f"keyint={gop_frames}:min-keyint={gop_frames}:scenecut=0:log-level=error",
                "-preset", "ultrafast"]
    if offset is not None:
        cmd += ["-output_ts_offset", str(offset)]
    cmd += ["-f", "mpegts", path]
    subprocess.run(cmd, check=True)
    with open(path, "rb") as f:
        return f.read()


def _observe(*sessions, chunk=None):
    """Feed each byte string as its own upstream session; return the session summaries."""
    out = []
    obs = TsObserver(on_session_end=out.append)
    for data in sessions:
        obs.new_session("test")
        if chunk:
            for i in range(0, len(data), chunk):
                obs.feed(data[i:i + chunk])
        else:
            obs.feed(data)
    obs.end_session()
    return out


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class TsObserverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        p = lambda name: os.path.join(cls.tmp, name)
        cls.base = _make_ts(p("base.ts"))                       # PTS starts ~1.4s, 2s GOP, B-frames
        cls.cont = _make_ts(p("cont.ts"), offset=10)            # picks up where base ends
        cls.fwd = _make_ts(p("fwd.ts"), offset=200)
        cls.late = _make_ts(p("late.ts"), offset=100)
        cls.wrap = _make_ts(p("wrap.ts"), offset=95438)         # crosses the 33-bit PTS wrap

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_codecs_gop_and_clean_stream(self):
        (s,) = _observe(self.base)
        self.assertEqual(s["video"], "h264")
        self.assertEqual(s["audio"], ["aac"])
        self.assertAlmostEqual(s["gop_avg_s"], 2.0, places=1)
        self.assertEqual(s["first_idr_s"], 0.0)
        self.assertAlmostEqual(s["media_s"], 10.0, delta=0.2)
        for k in ("jumps_fwd", "jumps_back", "cc_errors", "tei", "resyncs", "pmt_changes"):
            self.assertEqual(s[k], 0, k)
        self.assertEqual(s["boundary"], "first")

    def test_chunking_does_not_change_results(self):
        (whole,) = _observe(self.base)
        (chunked,) = _observe(self.base, chunk=1000)
        for k in ("media_s", "gop_avg_s", "first_idr_s", "cc_errors", "jumps_fwd", "jumps_back", "video"):
            self.assertEqual(whole[k], chunked[k], k)

    def test_boundary_classification(self):
        _, cont = _observe(self.base, self.cont)
        self.assertEqual(cont["boundary"], "continuous")
        self.assertEqual(cont["video"], "h264")              # codecs recorded on every session
        self.assertEqual(cont["audio"], ["aac"])

        _, fwd = _observe(self.base, self.fwd)
        self.assertEqual(fwd["boundary"], "forward")
        self.assertGreater(fwd["boundary_s"], 100)

        _, rewind = _observe(self.base, self.base)              # provider replays what was already sent
        self.assertEqual(rewind["boundary"], "rewind")
        self.assertAlmostEqual(rewind["boundary_s"], -10.0, delta=0.5)

        _, reset = _observe(self.late, self.base)               # timeline restarts far in the past
        self.assertEqual(reset["boundary"], "reset")

    def test_pts_wrap_is_not_a_jump(self):
        (s,) = _observe(self.wrap)
        self.assertEqual(s["jumps_fwd"], 0)
        self.assertEqual(s["jumps_back"], 0)
        self.assertAlmostEqual(s["media_s"], 10.0, delta=0.2)
        self.assertEqual(pts_delta(PTS_WRAP - 100, 50), 150)

    def test_mid_gop_start_measures_wait_for_keyframe(self):
        cut = (len(self.base) // 3) // TS_PACKET * TS_PACKET
        (s,) = _observe(self.base[cut:])
        self.assertGreater(s["first_idr_s"], 0.0)
        self.assertLess(s["first_idr_s"], 2.1)
        self.assertGreater(s["first_idr_bytes"], 0)

    def test_dropped_packets_counted_as_cc_errors(self):
        data = bytearray(self.base)
        mid = (len(data) // 2) // TS_PACKET * TS_PACKET
        del data[mid:mid + 20 * TS_PACKET]
        (s,) = _observe(bytes(data))
        self.assertGreater(s["cc_errors"], 0)

    def test_resyncs_on_misaligned_input(self):
        (s,) = _observe(b"\x00" * 50 + self.base)
        self.assertGreaterEqual(s["resyncs"], 1)
        self.assertEqual(s["video"], "h264")
        self.assertAlmostEqual(s["gop_avg_s"], 2.0, places=1)

    def test_hevc_keyframes(self):
        if not _has_encoder("libx265"):
            self.skipTest("libx265 not available")
        data = _make_ts(os.path.join(self.tmp, "hevc.ts"), vcodec="libx265")
        (s,) = _observe(data)
        self.assertEqual(s["video"], "hevc")
        self.assertAlmostEqual(s["gop_avg_s"], 2.0, places=1)


def _has_encoder(name):
    out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return f" {name} " in out


if __name__ == "__main__":
    unittest.main()
