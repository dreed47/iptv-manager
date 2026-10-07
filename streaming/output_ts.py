"""Continuous MPEG-TS HTTP output from a ChannelEngine (Plex HDHomeRun, Xtream .ts, VLC).

Each response starts with PAT/PMT followed by a video keyframe, so the client always has
something it can begin decoding. The body is an async generator: when the client goes
away Starlette cancels it at the next await, the finally block detaches the viewer at
once, and the engine's idle timer takes over (the old sync generators kept pulling from
the provider for ~20s after a client left).

The response is only started once the engine has keyframe-aligned data, so a channel
that can't be tuned gets a prompt error status instead of a hanging 200.
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

import config
from streaming import registry
from streaming.engine import ChannelEngine, Consumer
from streaming.ringbuffer import EOS, SKIPPED

logger = logging.getLogger(__name__)


async def ts_response(*, item_id: int, url: str, label: str, max_sessions: int, client_ip: str,
                      user_agent: str, channel: str, channel_name: str, source: str,
                      busy_status: int) -> StreamingResponse:
    consumer = Consumer(client_ip=client_ip, user_agent=user_agent, source=source,
                        channel=channel, channel_name=channel_name, item_id=item_id)
    try:
        engine = registry.acquire(item_id, url, label, max_sessions, consumer)
    except registry.BudgetExceeded as exc:
        logger.warning(f"Stream rejected [{label}] for {client_ip}: {exc}")
        raise HTTPException(status_code=busy_status, detail=f"All provider connections in use ({exc})",
                            headers={"Retry-After": "10"})

    deadline = time.monotonic() + config.ENGINE_START_TIMEOUT
    while not engine.ring.joinable:
        if engine.finished or time.monotonic() > deadline:
            registry.release(engine, consumer)
            reason = engine.failure or f"no keyframe within {config.ENGINE_START_TIMEOUT}s"
            logger.warning(f"Stream start failed [{label}] for {client_ip}: {reason}")
            raise HTTPException(status_code=503, detail=f"Channel unavailable: {reason}")
        await asyncio.sleep(0.1)

    logger.info(f"Viewer joined [{label}] {source} {client_ip} ({len(engine.consumers)} watching)")
    return StreamingResponse(_stream(engine, consumer), media_type="video/mp2t",
                             headers={"Cache-Control": "no-cache, no-store"})


async def _stream(engine: ChannelEngine, consumer: Consumer):
    ring = engine.ring
    reason = "client disconnected"
    try:
        cursor, header = ring.join()
        yield header
        while True:
            if consumer.killed:
                reason = "killed by admin"
                break
            data, cursor, status = ring.read(cursor)
            if status == EOS:
                reason = "channel ended"
                break
            if status == SKIPPED:
                logger.warning(f"Viewer fell behind [{engine.label}] {consumer.client_ip}; jumped to latest keyframe")
            if data:
                consumer.bytes_sent += len(data)
                consumer.last_chunk_at = time.time()
                yield data
            else:
                await ring.wait(cursor, 1.0)
    finally:
        registry.release(engine, consumer)
        logger.info(f"Viewer left [{engine.label}] {consumer.client_ip}: {reason}, "
                    f"{consumer.bytes_sent / 1048576:.1f}MB in {time.time() - consumer.started_at:.0f}s")
