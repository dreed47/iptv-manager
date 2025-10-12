from fastapi import FastAPI
from models import init_db
from routes import router
from hdhomerun_routes import router as hdhomerun_router
import threading
import logging

logger = logging.getLogger(__name__)

def create_app():
    app = FastAPI(title="IPTV Manager with HDHomeRun Emulation")
    
    init_db()
    
    app.include_router(router)
    app.include_router(hdhomerun_router)
    
    return app

app = create_app()
