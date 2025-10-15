import os
import logging
from sqlalchemy import create_engine, Column, Integer, String, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
# Use data directory for database
DATA_DIR = os.path.join(BASE_DIR, 'data')
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'data.db')
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={
        "check_same_thread": False,
        "timeout": 5  # 5 second timeout on connections
    },
    pool_pre_ping=True,  # Verify connection is still valid before using
    pool_recycle=3600,  # Recycle connections after 1 hour
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Item(Base):
    __tablename__ = "items"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    server_url = Column(String(200), nullable=False)
    username = Column(String(200), nullable=False)
    user_pass = Column(String(200), nullable=False)
    languages = Column(String(200), nullable=True)
    includes = Column(String(200), nullable=True)
    excludes = Column(String(200), nullable=True)
    epg_channels = Column(String(1000), nullable=True)

def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = None
    try:
        db = SessionLocal()
        # Test connection immediately using SQLAlchemy text()
        from sqlalchemy import text
        db.execute(text("SELECT 1"))
        yield db
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        if db:
            db.close()
        # Return a new session if the first one failed
        db = SessionLocal()
        yield db
    finally:
        if db:
            db.close()
