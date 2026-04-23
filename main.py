import logging
import time
import collections
import threading

_LOG_BUFFER_LOCK = threading.Lock()
_LOG_BUFFER: collections.deque = collections.deque(maxlen=2000)

class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "ts": record.created,
            "level": record.levelname,
            "name": record.name,
            "msg": self.format(record),
        }
        with _LOG_BUFFER_LOCK:
            _LOG_BUFFER.append(entry)

def get_log_buffer():
    with _LOG_BUFFER_LOCK:
        return list(_LOG_BUFFER)

# Configure logging before any other imports so basicConfig calls in sub-modules become no-ops
def _configure_logging():
    fmt = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %z",
    )
    fmt.converter = time.localtime  # respects TZ environment variable
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    buf_handler = _BufferHandler()
    buf_handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %z",
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(stream_handler)
    root.addHandler(buf_handler)

_configure_logging()

import asyncio
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from models import init_db, SessionLocal
from routes import router
from hdhomerun_routes import router as hdhomerun_router
from xtream_server_routes import router as xtream_server_router, get_xtream_cache
from health_routes import router as health_router
from m3u_service import start_m3u_scheduler

logger = logging.getLogger(__name__)

def create_app():
    app = FastAPI(
        title="IPTV Manager — HDHomeRun Emulator & Xtream Proxy",
        # Disable docs to prevent OpenAPI spec generation delays
        docs_url=None,
        redoc_url=None
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Initialize database in a background task to avoid blocking startup
    async def _warm_xtream_cache():
        try:
            with SessionLocal() as db:
                await get_xtream_cache(db)
            logger.info("Xtream cache pre-warm complete")
        except Exception as exc:
            logger.warning(f"Xtream cache pre-warm failed: {exc}")

    @app.on_event("startup")
    async def startup_event():
        logger.info("Starting application...")
        init_db()
        logger.info("Database initialized")
        start_m3u_scheduler()
        logger.info("M3U scheduler started")
        # Warm the Xtream cache in the background so the first stream request
        # doesn't block for ~28s while 180K+ VOD/series entries are indexed.
        asyncio.create_task(_warm_xtream_cache())
    
    @app.middleware("http")
    async def log_request_time(request: Request, call_next):
        start = time.perf_counter()
        path = request.url.path
        # Only log non-static requests to reduce noise
        if not path.startswith(("/static/", "/favicon.ico")):
            logger.info(f"--> START {request.method} {path}")
        try:
            response = await call_next(request)
            return response
        finally:
            duration = time.perf_counter() - start
            if not path.startswith(("/static/", "/favicon.ico")):
                logger.info(f"<-- END   {request.method} {path}  duration={duration:.3f}s")

    logger.info("Application initialized, routing configured")

    app.include_router(router)
    app.include_router(hdhomerun_router)
    app.include_router(xtream_server_router)
    app.include_router(health_router)
    logger.info("Application routes configured")
    
    return app

app = create_app()
