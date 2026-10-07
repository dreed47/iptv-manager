import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import config
from streaming import metrics, report
from tests.test_tsfix import FFMPEG, _make_ts

URL = "http://cdn.example/live/user/pass/324923.ts"


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class MetricsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.base = _make_ts(os.path.join(cls.tmp, "base.ts"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.log = os.path.join(self.tmp, "obs.jsonl")
        if os.path.exists(self.log):
            os.remove(self.log)
        metrics._idle.clear()
        metrics._recent.clear()
        patcher = mock.patch.object(config, "STREAM_OBSERVE_LOG", self.log)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _play(self, url=URL):
        obs = metrics.acquire(1, url, "ch 15 'FOX NEWS'", "hdhr")
        obs.new_session("initial")
        obs.feed(self.base)
        metrics.release(obs)
        return obs

    def test_client_reconnect_continues_channel_and_measures_boundary(self):
        first = self._play()
        # Same channel, rotated domain/token: still the same channel.
        second = self._play("http://other.example/live/user/pass/324923.ts?token=x")
        self.assertIs(first, second)
        recs = report.load(self.log)
        self.assertEqual([r["boundary"] for r in recs], ["first", "rewind"])
        self.assertEqual(recs[0]["stream_id"], "324923")

    def test_concurrent_consumers_get_separate_observers(self):
        a = metrics.acquire(1, URL, "x", "hdhr")
        b = metrics.acquire(1, URL, "x", "hdhr")
        self.assertIsNot(a, b)

    def test_observer_errors_never_propagate(self):
        obs = metrics.acquire(1, URL, "x", "hdhr")
        with mock.patch.object(obs._observer, "feed", side_effect=RuntimeError("boom")):
            with self.assertLogs("streaming.metrics", level="ERROR"):
                obs.feed(b"\x47" * 188)
        obs.feed(self.base)        # silently ignored once disabled
        metrics.release(obs)
        self.assertIsNone(obs.snapshot())

    def test_report_summarizes_per_channel(self):
        self._play()
        self._play()
        (row,) = report.summarize(report.load(self.log))
        self.assertEqual(row["sessions"], 2)
        self.assertEqual(row["boundaries"], {"first": 1, "rewind": 1})
        self.assertEqual(row["codecs"], ["h264/aac"])
        with open(self.log) as f:
            self.assertEqual(len([json.loads(l) for l in f]), 2)


if __name__ == "__main__":
    unittest.main()
