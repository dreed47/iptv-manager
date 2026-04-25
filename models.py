import os
import logging
from sqlalchemy import create_engine, Column, Integer, String, Boolean, Text
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
    xtream_includes = Column(String(10000), nullable=True)
    m3u_refresh_hours = Column(Integer, nullable=True, default=0)
    epg_channels = Column(String(1000), nullable=True)
    provider_status = Column(String(50), nullable=True)
    provider_exp_date = Column(String(50), nullable=True)
    vpn_enabled   = Column(Boolean, nullable=True, default=False)
    vpn_config    = Column(Text, nullable=True)
    vpn_username  = Column(String(500), nullable=True)
    vpn_password  = Column(String(500), nullable=True)
    max_sessions  = Column(Integer, nullable=True, default=1)
    mqtt_enabled       = Column(Boolean, nullable=True, default=False)
    mqtt_host          = Column(String(200), nullable=True)
    mqtt_port          = Column(Integer, nullable=True, default=1883)
    mqtt_username      = Column(String(200), nullable=True)
    mqtt_password      = Column(String(500), nullable=True)
    mqtt_topic_prefix  = Column(String(200), nullable=True, default="iptv-manager")
    mqtt_ha_discovery  = Column(Boolean, nullable=True, default=False)
    mqtt_device_name   = Column(String(200), nullable=True, default="IPTV Manager")

def init_db():
    Base.metadata.create_all(bind=engine)
    # Migrate existing databases that predate new columns
    with engine.connect() as conn:
        from sqlalchemy import text, inspect
        existing = [col["name"] for col in inspect(engine).get_columns("items")]
        if "xtream_includes" not in existing:
            conn.execute(text("ALTER TABLE items ADD COLUMN xtream_includes VARCHAR(10000)"))
            conn.commit()
            logger.info("Migrated: added xtream_includes column")
        if "m3u_refresh_hours" not in existing:
            conn.execute(text("ALTER TABLE items ADD COLUMN m3u_refresh_hours INTEGER DEFAULT 0"))
            conn.commit()
            logger.info("Migrated: added m3u_refresh_hours column")
        if "provider_status" not in existing:
            conn.execute(text("ALTER TABLE items ADD COLUMN provider_status VARCHAR(50)"))
            conn.commit()
            logger.info("Migrated: added provider_status column")
        if "provider_exp_date" not in existing:
            conn.execute(text("ALTER TABLE items ADD COLUMN provider_exp_date VARCHAR(50)"))
            conn.commit()
            logger.info("Migrated: added provider_exp_date column")
        if "vpn_enabled" not in existing:
            conn.execute(text("ALTER TABLE items ADD COLUMN vpn_enabled BOOLEAN DEFAULT 0"))
            conn.commit()
            logger.info("Migrated: added vpn_enabled column")
        if "vpn_config" not in existing:
            conn.execute(text("ALTER TABLE items ADD COLUMN vpn_config TEXT"))
            conn.commit()
            logger.info("Migrated: added vpn_config column")
        if "vpn_username" not in existing:
            conn.execute(text("ALTER TABLE items ADD COLUMN vpn_username VARCHAR(500)"))
            conn.commit()
            logger.info("Migrated: added vpn_username column")
        if "vpn_password" not in existing:
            conn.execute(text("ALTER TABLE items ADD COLUMN vpn_password VARCHAR(500)"))
            conn.commit()
            logger.info("Migrated: added vpn_password column")
        if "max_sessions" not in existing:
            conn.execute(text("ALTER TABLE items ADD COLUMN max_sessions INTEGER DEFAULT 1"))
            conn.commit()
            logger.info("Migrated: added max_sessions column")
        if "mqtt_enabled" not in existing:
            conn.execute(text("ALTER TABLE items ADD COLUMN mqtt_enabled BOOLEAN DEFAULT 0"))
            conn.commit()
            logger.info("Migrated: added mqtt_enabled column")
        if "mqtt_host" not in existing:
            conn.execute(text("ALTER TABLE items ADD COLUMN mqtt_host VARCHAR(200)"))
            conn.commit()
            logger.info("Migrated: added mqtt_host column")
        if "mqtt_port" not in existing:
            conn.execute(text("ALTER TABLE items ADD COLUMN mqtt_port INTEGER DEFAULT 1883"))
            conn.commit()
            logger.info("Migrated: added mqtt_port column")
        if "mqtt_username" not in existing:
            conn.execute(text("ALTER TABLE items ADD COLUMN mqtt_username VARCHAR(200)"))
            conn.commit()
            logger.info("Migrated: added mqtt_username column")
        if "mqtt_password" not in existing:
            conn.execute(text("ALTER TABLE items ADD COLUMN mqtt_password VARCHAR(500)"))
            conn.commit()
            logger.info("Migrated: added mqtt_password column")
        if "mqtt_topic_prefix" not in existing:
            conn.execute(text("ALTER TABLE items ADD COLUMN mqtt_topic_prefix VARCHAR(200) DEFAULT 'iptv-manager'"))
            conn.commit()
            logger.info("Migrated: added mqtt_topic_prefix column")
        if "mqtt_ha_discovery" not in existing:
            conn.execute(text("ALTER TABLE items ADD COLUMN mqtt_ha_discovery BOOLEAN DEFAULT 0"))
            conn.commit()
            logger.info("Migrated: added mqtt_ha_discovery column")
        if "mqtt_device_name" not in existing:
            conn.execute(text("ALTER TABLE items ADD COLUMN mqtt_device_name VARCHAR(200) DEFAULT 'IPTV Manager'"))
            conn.commit()
            logger.info("Migrated: added mqtt_device_name column")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
