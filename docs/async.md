## Async rules

All route handlers are `async def`. Any blocking I/O (file reads, `apply_m3u_filter` on large files, EPG processing) must run in `await asyncio.to_thread(fn)` — direct blocking calls in async handlers stall the entire event loop. Background EPG rebuilds use `BackgroundTasks.add_task(_do_epg_refresh)`.
