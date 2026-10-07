import asyncio
import threading
import unittest

from streaming.ringbuffer import EOS, OK, SKIPPED, RingBuffer


class RingBufferTest(unittest.IsolatedAsyncioTestCase):
    def test_join_starts_at_latest_gop_with_its_header(self):
        r = RingBuffer(1 << 20)
        self.assertIsNone(r.join())
        r.append(b"a", None)
        r.append(b"G1", b"H1")
        r.append(b"b", None)
        r.append(b"G2", b"H2")
        r.append(b"c", None)
        cursor, header = r.join()
        self.assertEqual(header, b"H2")
        data, cursor, status = r.read(cursor)
        self.assertEqual((data, status), (b"G2c", OK))
        self.assertEqual(r.read(cursor), (b"", cursor, OK))

    def test_fallen_behind_consumer_jumps_to_latest_gop_with_header(self):
        r = RingBuffer(10)
        r.append(b"Gxxx", b"H1")
        cursor, _ = r.join()
        for _ in range(5):
            r.append(b"yyyy", None)
        r.append(b"Gzzz", b"H2")
        data, _, status = r.read(cursor)
        self.assertEqual(status, SKIPPED)
        self.assertTrue(data.startswith(b"H2Gzzz"))

    def test_eos_after_drain(self):
        r = RingBuffer(1 << 20)
        r.append(b"G", b"H")
        r.close()
        cursor, _ = r.join()
        data, cursor, status = r.read(cursor)
        self.assertEqual((data, status), (b"G", OK))
        self.assertEqual(r.read(cursor)[2], EOS)

    async def test_wait_wakes_on_append_from_thread(self):
        r = RingBuffer(1 << 20)
        r.append(b"G", b"H")
        _, cursor, _ = r.read(r.join()[0])
        threading.Timer(0.05, r.append, args=(b"next", None)).start()
        await asyncio.wait_for(r.wait(cursor, 5.0), 2.0)
        self.assertEqual(r.read(cursor)[0], b"next")

    async def test_wait_times_out(self):
        r = RingBuffer(1 << 20)
        r.append(b"G", b"H")
        _, cursor, _ = r.read(r.join()[0])
        await asyncio.wait_for(r.wait(cursor, 0.05), 1.0)


if __name__ == "__main__":
    unittest.main()
