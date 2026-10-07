"""End-to-end tests of the streaming engine: real FFmpeg, a local HTTP server standing in
for the IPTV provider, consumers driven through output_ts exactly as the routes do."""
import asyncio
import json
import os
import subprocess
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from fastapi import HTTPException

import config
from streaming import engine as engine_mod
from streaming import output_ts, registry
from streaming.tsfix import TS_PACKET, TsObserver
from tests.test_tsfix import FFMPEG, _make_ts


class _Provider(ThreadingHTTPServer):
    """A live channel at /live/u/p/<id>.ts. Its live edge advances in real time through
    `payload` (starting `live_start` seconds in). Like real providers, every connection
    starts `replay` seconds behind the live edge and then follows it. `script[n]` sets how
    connection n misbehaves:
        speed=0.4         deliver at 0.4x real time
        close_after=4     drop the connection after 4s
        rewind_after=4    after 4s jump back `rewind` seconds (default 8) mid-connection
        stall_after=4     after 4s go silent, connection left open
        status=404        refuse the connection
    `script` may also be a dict of stream id → list, to script each stream separately.
    Once the payload is used up, new connections get 404 (the channel is gone)."""
    daemon_threads = True

    def __init__(self, payload: bytes, seconds: float, script=(), live_start=3.0, replay=3.0):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.payload = payload
        self.rate = len(payload) / seconds
        self.script = script if isinstance(script, dict) else list(script)
        self.per_stream: dict[int, int] = {}
        self.live_start = live_start
        self.replay = replay
        self.t0 = time.monotonic()
        self.connections = 0
        self.served = 0
        self.closing = threading.Event()
        self.lock = threading.Lock()

    def live_edge(self) -> int:
        return min(len(self.payload), int((self.live_start + time.monotonic() - self.t0) * self.rate))

    def at(self, pos: float) -> int:
        return max(0, int(pos)) // TS_PACKET * TS_PACKET


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        srv = self.server
        stream_id = int(self.path.rsplit("/", 1)[-1].split(".")[0]) if self.path[-1].isdigit() or ".ts" in self.path else 0
        with srv.lock:
            srv.connections += 1
            per = srv.per_stream.setdefault(stream_id, 0)
            srv.per_stream[stream_id] += 1
            n = per if isinstance(srv.script, dict) else srv.connections - 1
        script = srv.script.get(stream_id, []) if isinstance(srv.script, dict) else srv.script
        how = script[n] if n < len(script) else {}
        if "404" in self.path or how.get("status") or srv.live_edge() >= len(srv.payload):
            self.send_error(how.get("status", 404))
            return
        with srv.lock:
            srv.served += 1
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.end_headers()
        pos = srv.at(srv.live_edge() - srv.replay * srv.rate)
        began = time.monotonic()
        rewound = False
        try:
            while not srv.closing.is_set():
                age = time.monotonic() - began
                if age > how.get("close_after", 1e9):
                    return
                if age > how.get("stall_after", 1e9):
                    srv.closing.wait(30)
                    return
                if age > how.get("rewind_after", 1e9) and not rewound:
                    pos, rewound = srv.at(pos - how.get("rewind", 8) * srv.rate), True
                if pos >= len(srv.payload):
                    return
                size = min(64 * 1024, srv.live_edge() - pos)
                if size <= 0:
                    time.sleep(0.02)
                    continue
                self.wfile.write(srv.payload[pos:pos + size])
                pos += size
                if "speed" in how:
                    time.sleep(size / (srv.rate * how["speed"]))
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


def _decode_problems(data: bytes, tmp: str) -> list[str]:
    """Decode the stream and check every timeline: no errors, no repeats, no holes."""
    path = os.path.join(tmp, "out.ts")
    with open(path, "wb") as f:
        f.write(data)
    problems = [line for line in subprocess.run(
        [FFMPEG, "-v", "error", "-i", path, "-f", "null", "-"], capture_output=True, text=True
    ).stderr.splitlines() if line.strip()]
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "packet=codec_type,dts_time",
                          "-of", "json", "-show_packets", path], capture_output=True, text=True).stdout
    by_type: dict[str, list[float]] = {}
    for p in json.loads(out)["packets"]:
        if p.get("dts_time") not in (None, "N/A"):
            by_type.setdefault(p["codec_type"], []).append(float(p["dts_time"]))
    for kind, dts in by_type.items():
        for a, b in zip(dts, dts[1:]):
            if not 0 < b - a < 0.25:
                problems.append(f"{kind} timeline {a:.3f} -> {b:.3f}")
    return problems


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class EngineTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.payload_seconds = 10.0
        cls.payload = _make_ts(os.path.join(cls.tmp, "src.ts"), seconds=cls.payload_seconds)
        cls.long_seconds = 60.0
        cls.long = _make_ts(os.path.join(cls.tmp, "long.ts"), seconds=cls.long_seconds)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        for name, value in {
            "STREAM_OBSERVE": False, "ENGINE_IDLE_SECS": 0.3, "ENGINE_START_RETRIES": 0,
            "ENGINE_START_TIMEOUT": 15, "ENGINE_AUDIO_CODEC": "ac3", "ENGINE_OUTAGE_SECS": 2,
            "ENGINE_STALL_SECS": 2, "ENGINE_SPEED_GRACE": 1, "ENGINE_SPEED_WINDOW": 3,
            "ENGINE_KEEPALIVE_SECS": 1, "ENGINE_FAILOVER": True, "ENGINE_FAILOVER_AFTER": 2,
            "ENGINE_FAILOVER_WINDOW": 120, "ENGINE_FAILOVER_COOLDOWN": 900,
            "ENGINE_FAILOVER_PREFIXES": "US,VIP", "M3U_DIR": self.tmp,
        }.items():
            p = mock.patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(registry.stop_all)
        engine_mod._feed_bad_until.clear()
        self.serve(self.payload, self.payload_seconds)

    def serve(self, payload, seconds, script=()):
        self.provider = _Provider(payload, seconds, script)
        threading.Thread(target=self.provider.serve_forever, daemon=True).start()
        self.addCleanup(self.provider.server_close)
        self.addCleanup(self.provider.shutdown)
        self.addCleanup(self.provider.closing.set)

    def url(self, stream_id):
        return f"http://127.0.0.1:{self.provider.server_address[1]}/live/u/p/{stream_id}.ts"

    async def open(self, stream_id, client_ip="10.0.0.1", max_sessions=2, name=None):
        return await output_ts.ts_response(
            item_id=1, url=self.url(stream_id), label=f"test {stream_id}", max_sessions=max_sessions,
            client_ip=client_ip, user_agent="test", channel=str(stream_id),
            channel_name=name or f"Test {stream_id}", source="test", busy_status=503,
        )

    async def test_viewer_gets_keyframe_aligned_normalized_stream_that_ends_when_channel_is_gone(self):
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
        self.assertEqual(self.provider.served, 1)

    async def test_budget_rejects_other_clients_but_lets_a_client_switch_channels(self):
        first = await self.open(1001, "10.0.0.1", max_sessions=1)
        with self.assertRaises(HTTPException) as ctx:
            await self.open(1002, "10.0.0.2", max_sessions=1)
        self.assertEqual(ctx.exception.status_code, 503)

        switched = await self.open(1002, "10.0.0.1", max_sessions=1)   # same client changes channel
        old = await _collect(first, max_seconds=10)                       # old channel's stream ends
        self.assertLess(len(old), len(self.payload))
        self.assertGreater(len(await _collect(switched)), 50_000)

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

    # ---- seamless reconnects ------------------------------------------------------

    async def watch(self, script, seconds=14):
        """Watch a 60s live channel whose connections misbehave per `script`."""
        self.serve(self.long, self.long_seconds, script)
        resp = await self.open(2001)
        data = await _collect(resp, max_seconds=seconds)
        engine = next(iter(registry._engines.values()))
        return data, engine

    def assertSeamless(self, data, engine, min_media, trims_replay=True):
        self.assertEqual(_decode_problems(data, self.tmp), [])
        s = _summary(data)
        self.assertEqual((s["jumps_fwd"], s["jumps_back"], s["cc_errors"]), (0, 0, 0))
        self.assertGreaterEqual(s["media_s"], min_media)
        self.assertGreaterEqual(engine.reconnects, 1)
        self.assertGreaterEqual(self.provider.served, 2)
        if trims_replay:
            # reconnecting right away lands ~3s back; that must be trimmed, not shown twice
            self.assertGreaterEqual(engine.timeline.skipped_seconds, 1.0 * engine.reconnects)
        return s

    async def test_dropped_connection_is_replaced_seamlessly(self):
        data, engine = await self.watch([{"close_after": 4}, {"close_after": 4}])
        self.assertSeamless(data, engine, min_media=10)
        self.assertGreaterEqual(engine.reconnects, 2)

    async def test_slow_connection_is_replaced(self):
        data, engine = await self.watch([{"speed": 0.4}])
        self.assertSeamless(data, engine, min_media=9, trims_replay=False)   # the slow one fell behind live

    async def test_rewinding_connection_is_replaced_without_showing_the_replay(self):
        data, engine = await self.watch([{"rewind_after": 4}])
        self.assertSeamless(data, engine, min_media=10)

    async def test_stalled_connection_is_replaced_and_viewer_kept_alive(self):
        data, engine = await self.watch([{"stall_after": 4}])
        self.assertSeamless(data, engine, min_media=8, trims_replay=False)   # live moved on during the stall
        null_packets = sum(1 for i in range(0, len(data), TS_PACKET)
                           if data[i + 1] & 0x1F == 0x1F and data[i + 2] == 0xFF)
        self.assertGreater(null_packets, 0, "viewer should get keepalive packets during the stall")

    async def test_engine_gives_up_after_outage(self):
        self.serve(self.long, self.long_seconds, [{"close_after": 3}] + [{"status": 503}] * 50)
        resp = await self.open(2001)
        started = time.monotonic()
        data = await _collect(resp, max_seconds=30)
        self.assertLess(time.monotonic() - started, 15, "stream should end once the outage limit passes")
        self.assertEqual(_decode_problems(data, self.tmp), [])

    # ---- failover to backup feeds -------------------------------------------------

    def news_channel(self, script):
        """Channel 'US: TEST NEWS HD' (3001) with a backup copy 'VIP: TEST NEWS' (3002) on the
        same account, plus a foreign copy that must never be used."""
        self.serve(self.long, self.long_seconds, script)
        port = self.provider.server_address[1]
        with open(os.path.join(self.tmp, "xtream_playlist_1.m3u"), "w") as f:
            f.write("#EXTM3U\n")
            for sid, name in ((3001, "US: TEST NEWS HD"), (3003, "AR: TEST NEWS"), (3002, "VIP: TEST NEWS")):
                f.write(f'#EXTINF:-1 tvg-id="{sid}" tvg-name="{name}",{name}\n'
                        f"http://127.0.0.1:{port}/live/u/p/{sid}.ts\n")

    async def test_failing_feed_switches_to_backup_seamlessly(self):
        self.news_channel({3001: [{"speed": 0.3}] * 20})
        resp = await self.open(3001, name="US: TEST NEWS HD")
        data = await _collect(resp, max_seconds=22)
        engine = next(iter(registry._engines.values()))
        self.assertEqual(engine.failovers, 1)
        self.assertTrue(engine.feed.url.endswith("/3002.ts"), engine.feed)
        self.assertEqual(self.provider.per_stream.get(3003), None, "foreign copy must not be used")
        self.assertEqual(_decode_problems(data, self.tmp), [])
        s = _summary(data)
        self.assertEqual((s["jumps_fwd"], s["jumps_back"], s["cc_errors"]), (0, 0, 0))
        self.assertIn("backup: VIP: TEST NEWS", str(registry.active_sessions()) + " backup: " + engine.feed.name)

        # the failed feed is remembered: tuning in again starts straight on the backup
        await resp.body_iterator.aclose()
        registry.stop_all()
        await self._wait_for(lambda: not registry._engines)
        resp = await self.open(3001, name="US: TEST NEWS HD")
        await _collect(resp, max_seconds=3)
        engine = next(iter(registry._engines.values()))
        self.assertTrue(engine.feed.url.endswith("/3002.ts"))
        self.assertEqual(self.provider.per_stream[3001], 2 + 0, "primary must not be retried during cooldown")

    async def test_unreachable_channel_starts_on_backup(self):
        self.news_channel({3001: [{"status": 404}] * 20})
        resp = await self.open(3001, name="US: TEST NEWS HD")
        data = await _collect(resp, max_seconds=6)
        self.assertGreater(len(data), 100_000)
        self.assertEqual(_decode_problems(data, self.tmp), [])

    async def test_no_backup_for_unmatched_channel(self):
        self.news_channel({})
        self.assertEqual(engine_mod.alternates.find(1, self.url(2001), "US: SOMETHING ELSE"), [])
        self.assertEqual(engine_mod.alternates.find(1, self.url(3001), "24/7: TEST NEWS"), [])

    async def _wait_for(self, cond, timeout=10):
        deadline = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > deadline:
                self.fail("condition not reached")
            await asyncio.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
