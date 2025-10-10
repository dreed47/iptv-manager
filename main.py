# main.py
from fastapi import FastAPI
from models import init_db
from routes import router

def create_app():
    app = FastAPI(title="FastAPI IPTV Manager")
    
    # Initialize database
    init_db()
    
    # Include routes
    app.include_router(router)
    
    return app

app = create_app()
