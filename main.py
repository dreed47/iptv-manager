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
    import os
    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
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
    root.setLevel(level)
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

    async def _autostart_vpn():
        try:
            from models import Item
            import vpn_manager
            with SessionLocal() as db:
                item = db.query(Item).first()
                if item and item.vpn_enabled and item.vpn_config and item.vpn_username and item.vpn_password:
                    logger.info("VPN auto-start: enabled flag set — connecting…")
                    ok, msg = await asyncio.to_thread(
                        vpn_manager.start_vpn,
                        item.vpn_config, item.vpn_username, item.vpn_password,
                    )
                    if ok:
                        logger.info(f"VPN auto-start: {msg}")
                    else:
                        logger.warning(f"VPN auto-start failed: {msg}")
        except Exception as exc:
            logger.warning(f"VPN auto-start error: {exc}")

    async def _autostart_mqtt():
        # Retry delays in seconds: immediate, then back off up to ~10 minutes total.
        # Handles the case where the MQTT broker container starts after this app
        # (common when both LXC containers restart together after a backup).
        _RETRY_DELAYS = [0, 15, 30, 60, 120, 300]
        try:
            from models import Item
            import mqtt_manager
            with SessionLocal() as db:
                item = db.query(Item).first()
                if not (item and item.mqtt_enabled and item.mqtt_host):
                    return
                mqtt_config = {
                    "mqtt_host": item.mqtt_host,
                    "mqtt_port": item.mqtt_port,
                    "mqtt_username": item.mqtt_username,
                    "mqtt_password": item.mqtt_password,
                    "mqtt_topic_prefix": item.mqtt_topic_prefix or "iptv-manager",
                    "mqtt_ha_discovery": item.mqtt_ha_discovery,
                    "mqtt_device_name": item.mqtt_device_name or "IPTV Manager",
                }
            logger.info("MQTT auto-start: enabled flag set — connecting…")
            for attempt, delay in enumerate(_RETRY_DELAYS):
                if delay:
                    logger.info(f"MQTT auto-start: retrying in {delay}s (attempt {attempt + 1}/{len(_RETRY_DELAYS)})…")
                    await asyncio.sleep(delay)
                if mqtt_manager.get_mqtt_status().get("connected"):
                    return  # connected in the UI while we were waiting
                ok, msg = await asyncio.to_thread(mqtt_manager.start_mqtt, mqtt_config)
                if ok:
                    logger.info(f"MQTT auto-start: {msg}")
                    return
                logger.warning(f"MQTT auto-start attempt {attempt + 1} failed: {msg}")
            logger.warning("MQTT auto-start: could not connect after all retry attempts — connect manually from Settings")
        except Exception as exc:
            logger.warning(f"MQTT auto-start error: {exc}")

    @app.on_event("startup")
    async def startup_event():
        logger.info("Starting application...")
        init_db()
        logger.info("Database initialized")
        start_m3u_scheduler()
        logger.info("M3U scheduler started")
        asyncio.create_task(_autostart_vpn())
        asyncio.create_task(_autostart_mqtt())
        # Warm the Xtream cache in the background so the first stream request
        # doesn't block for ~28s while 180K+ VOD/series entries are indexed.
        asyncio.create_task(_warm_xtream_cache())
    
    @app.middleware("http")
    async def log_request_time(request: Request, call_next):
        start = time.perf_counter()
        path = request.url.path
        try:
            response = await call_next(request)
            return response
        finally:
            duration = time.perf_counter() - start
            if not path.startswith(("/static/", "/favicon.ico")):
                logger.debug(f"{request.method} {path}  {duration:.3f}s")

    app.include_router(router)
    app.include_router(hdhomerun_router)
    app.include_router(xtream_server_router)
    app.include_router(health_router)
    
    return app

app = create_app()
