from fastapi import FastAPI
from models import init_db
from routes import router
from hdhomerun_routes import router as hdhomerun_router

import logging
import time
from fastapi import Request

logger = logging.getLogger(__name__)

def create_app():
    app = FastAPI(
        title="IPTV Manager with HDHomeRun Emulation",
        # Disable docs to prevent OpenAPI spec generation delays
        docs_url=None,
        redoc_url=None
    )
    
    # Initialize database in a background task to avoid blocking startup
    @app.on_event("startup")
    async def startup_event():
        # Perform quick startup
        logger.info("Starting application...")
        init_db()
        logger.info("Database initialized")
    
    @app.middleware("http")
    async def log_request_time(request: Request, call_next):
        start = time.time()
        path = request.url.path
        # Only log non-static requests to reduce noise
        if not path.startswith(("/static/", "/favicon.ico")):
            logger.info(f"--> START {request.method} {path}")
        try:
            response = await call_next(request)
            return response
        finally:
            duration = time.time() - start
            if not path.startswith(("/static/", "/favicon.ico")):
                logger.info(f"<-- END   {request.method} {path}  duration={duration:.3f}s")

    logger.info("Application initialized, routing configured")

    app.include_router(router)
    app.include_router(hdhomerun_router)
    logger.info("Application routes configured")
    
    return app

app = create_app()
