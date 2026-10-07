"""End-to-end tests of the streaming engine: real FFmpeg, a local HTTP server standing in
for the IPTV provider, consumers driven through output_ts exactly as the routes do."""
import asyncio
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from fastapi import HTTPException

import config
from streaming import output_ts, registry
from streaming.tsfix import TS_PACKET, TsObserver
from tests.test_tsfix import FFMPEG, _make_ts


class _Provider(ThreadingHTTPServer):
    """Serves /live/u/p/<id>.ts from a fixture at `speed`x real time; counts connections."""
    daemon_threads = True

    def __init__(self, payload: bytes, seconds: float, speed: float):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.payload = payload
        self.rate = len(payload) / seconds * speed
        self.connections = 0
        self.lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if "404" in self.path:
            self.send_error(404)
            return
        with self.server.lock:
            self.server.connections += 1
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.end_headers()
        data, step = self.server.payload, 64 * 1024
        try:
            for i in range(0, len(data), step):
                self.wfile.write(data[i:i + step])
                time.sleep(step / self.server.rate)
        except (BrokenPipeError, ConnectionResetError):
            pass


async def _collect(resp, max_seconds=30):
    out = bytearray()
    deadline = time.monotonic() + max_seconds
    async for chunk in resp.body_iterator:
        out += chunk
        if time.monotonic() > deadline:
            break
    return bytes(out)


def _summary(data: bytes) -> dict:
    obs = TsObserver()
    obs.feed(data)
    return obs.end_session()


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class EngineTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        full = _make_ts(os.path.join(cls.tmp, "src.ts"), seconds=12)
        cut = (len(full) // 3) // TS_PACKET * TS_PACKET    # provider stream starts mid-GOP
        cls.payload = full[cut:]
        cls.payload_seconds = 8.0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        for name, value in {
            "STREAM_OBSERVE": False, "ENGINE_IDLE_SECS": 0.3, "ENGINE_START_RETRIES": 0,
            "ENGINE_START_TIMEOUT": 15, "ENGINE_AUDIO_CODEC": "ac3",
        }.items():
            p = mock.patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.provider = _Provider(self.payload, self.payload_seconds, speed=2.0)
        threading.Thread(target=self.provider.serve_forever, daemon=True).start()
        self.addCleanup(self.provider.server_close)
        self.addCleanup(self.provider.shutdown)
        self.addCleanup(registry.stop_all)

    def url(self, stream_id):
        return f"http://127.0.0.1:{self.provider.server_address[1]}/live/u/p/{stream_id}.ts"

    async def open(self, stream_id, client_ip="10.0.0.1", max_sessions=2):
        return await output_ts.ts_response(
            item_id=1, url=self.url(stream_id), label=f"test {stream_id}", max_sessions=max_sessions,
            client_ip=client_ip, user_agent="test", channel=str(stream_id), channel_name=f"Test {stream_id}",
            source="test", busy_status=503,
        )

    async def test_viewer_gets_keyframe_aligned_normalized_stream_that_ends_with_upstream(self):
        resp = await self.open(1001)
        data = await _collect(resp)
        self.assertEqual(data[0], 0x47)
        self.assertEqual(((data[1] & 0x1F) << 8) | data[2], 0, "stream must begin with the PAT")
        s = _summary(data)
        self.assertEqual(s["video"], "h264")
        self.assertEqual(s["audio"], ["ac3"])
        self.assertEqual(s["first_idr_s"], 0.0, "first video frame must be a keyframe")
        self.assertEqual(s["cc_errors"], 0, "join header must flow into the stream's own PAT/PMT")
        self.assertEqual(s["jumps_back"], 0)
        self.assertGreater(s["media_s"], 2.0)          # joins at the latest keyframe, so less than the whole payload
        await self._wait_for(lambda: not registry.active_sessions() and not registry._engines)

    async def test_viewers_share_one_upstream_connection(self):
        a, b = await asyncio.gather(self.open(1001, "10.0.0.1"), self.open(1001, "10.0.0.2"))
        self.assertEqual(len(registry.active_sessions()), 2)
        da, db = await asyncio.gather(_collect(a), _collect(b))
        self.assertGreater(len(da), 100_000)
        self.assertGreater(len(db), 100_000)
        self.assertEqual(self.provider.connections, 1)

    async def test_budget_rejects_other_clients_but_lets_a_client_switch_channels(self):
        first = await self.open(1001, "10.0.0.1", max_sessions=1)
        with self.assertRaises(HTTPException) as ctx:
            await self.open(1002, "10.0.0.2", max_sessions=1)
        self.assertEqual(ctx.exception.status_code, 503)

        switched = await self.open(1002, "10.0.0.1", max_sessions=1)   # same client changes channel
        old = await _collect(first, max_seconds=10)                       # old channel's stream ends
        self.assertLess(len(old), len(self.payload))
        self.assertGreater(len(await _collect(switched)), 100_000)

    async def test_unavailable_channel_returns_503(self):
        with self.assertRaises(HTTPException) as ctx:
            await self.open(404)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("upstream connect failed", ctx.exception.detail)

    async def test_engine_stops_after_last_viewer_leaves(self):
        resp = await self.open(1001)
        it = resp.body_iterator
        await it.__anext__()
        await it.__anext__()
        await it.aclose()                                   # client disconnects
        self.assertEqual(registry.active_sessions(), [])
        await self._wait_for(lambda: not registry._engines, timeout=5)

    async def test_admin_kill_ends_stream_and_engine(self):
        resp = await self.open(1001)
        it = resp.body_iterator
        await it.__anext__()
        (session,) = registry.active_sessions()
        self.assertEqual(registry.kill(session["session_id"]), "10.0.0.1")
        rest = bytearray()
        async for chunk in it:
            rest += chunk
        await self._wait_for(lambda: not registry._engines, timeout=5)

    async def _wait_for(self, cond, timeout=10):
        deadline = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > deadline:
                self.fail("condition not reached")
            await asyncio.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
