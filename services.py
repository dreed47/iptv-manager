import logging
import os
from sqlalchemy.orm import Session
from models import Item

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# services.py
def create_item(db: Session, name: str, server_url: str, username: str, user_pass: str, languages: str, includes: str, excludes: str, xtream_includes: str = None, m3u_refresh_hours: int = 0, max_sessions: int = 1):
    try:
        db_item = Item(name=name, server_url=server_url, username=username, user_pass=user_pass, languages=languages, includes=includes, excludes=excludes, xtream_includes=xtream_includes, m3u_refresh_hours=m3u_refresh_hours, max_sessions=max_sessions)
        db.add(db_item)
        db.commit()
        db.refresh(db_item)
        logger.info(f"Created item with id {db_item.id} and name '{name}'")
        return db_item
    except Exception as e:
        logger.error(f"Failed to create item: {str(e)}")
        db.rollback()
        return None

def update_item(db: Session, item_id: int, name: str, server_url: str, username: str, user_pass: str, languages: str, includes: str, excludes: str, xtream_includes: str = None, m3u_refresh_hours: int = None, max_sessions: int = None):
    try:
        db_item = db.query(Item).filter(Item.id == item_id).first()
        if db_item:
            if name is not None:
                db_item.name = name
            if server_url is not None:
                db_item.server_url = server_url
            if username is not None:
                db_item.username = username
            if user_pass is not None:
                db_item.user_pass = user_pass
            if languages is not None:
                db_item.languages = languages
            if includes is not None:
                db_item.includes = includes
            if excludes is not None:
                db_item.excludes = excludes
            if xtream_includes is not None:
                db_item.xtream_includes = xtream_includes
            if m3u_refresh_hours is not None:
                db_item.m3u_refresh_hours = m3u_refresh_hours
            if max_sessions is not None:
                db_item.max_sessions = max_sessions
            db.commit()
            db.refresh(db_item)
            logger.info(f"Updated item with id {item_id}")
            return db_item
        logger.warning(f"Item with id {item_id} not found for update")
        return None
    except Exception as e:
        logger.error(f"Failed to update item: {str(e)}")
        db.rollback()
        return None
    
def get_all_items(db: Session):
    try:
        items = db.query(Item).all()
        logger.debug(f"Retrieved {len(items)} items from database")
        return items
    except Exception as e:
        logger.error(f"Failed to retrieve items: {str(e)}")
        return []


def get_item_context(db: Session, base_url: str, m3u_dir: str) -> dict | None:
    """Build the single-provider context dict with file-existence flags. Returns None if not configured."""
    item = db.query(Item).first()
    if not item:
        return None
    try:
        existing_files = set(os.listdir(m3u_dir))
    except Exception as e:
        logger.error(f"Error listing m3u_files directory: {e}")
        existing_files = set()

    refresh_h = item.m3u_refresh_hours
    item_dict = {
        'id': item.id,
        'name': item.name,
        'server_url': item.server_url,
        'username': item.username,
        'user_pass': item.user_pass,
        'languages': item.languages,
        'includes': item.includes,
        'excludes': item.excludes,
        'xtream_includes': item.xtream_includes,
        'epg_channels': item.epg_channels,
        'm3u_refresh_hours': (
            int(refresh_h) if isinstance(refresh_h, (int, str))
            and str(refresh_h).isdigit() else 0
        ),
        'has_m3u': f"xtream_playlist_{item.id}.m3u" in existing_files,
        'has_filtered': f"filtered_playlist_{item.id}.m3u" in existing_files,
        'has_epg': "generated_epg.xml" in existing_files,
        'stream_url': f"{base_url}/stream_filtered_m3u/{item.id}",
        'epg_url': f"{base_url}/epg.xml",
        'provider_status': item.provider_status,
        'provider_exp_date': item.provider_exp_date,
        'vpn_enabled': bool(item.vpn_enabled),
        'vpn_config': item.vpn_config or '',
        'vpn_username': item.vpn_username or '',
        'vpn_configured': bool(item.vpn_config and item.vpn_username and item.vpn_password),
        'max_sessions': int(item.max_sessions) if item.max_sessions is not None else 1,
        'mqtt_enabled': bool(item.mqtt_enabled),
        'mqtt_host': item.mqtt_host or '',
        'mqtt_port': int(item.mqtt_port) if item.mqtt_port is not None else 1883,
        'mqtt_username': item.mqtt_username or '',
        'mqtt_password': item.mqtt_password or '',
        'mqtt_topic_prefix': item.mqtt_topic_prefix or 'iptv-manager',
        'mqtt_ha_discovery': bool(item.mqtt_ha_discovery),
        'mqtt_device_name': item.mqtt_device_name or 'IPTV Manager',
        'mqtt_configured': bool(item.mqtt_host),
    }
    m3u_path = os.path.join(m3u_dir, f"xtream_playlist_{item.id}.m3u")
    try:
        item_dict['m3u_last_fetched_ts'] = int(os.path.getmtime(m3u_path))
    except FileNotFoundError:
        item_dict['m3u_last_fetched_ts'] = None
    return item_dict
