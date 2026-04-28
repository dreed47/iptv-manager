import re
import logging
import os
from sqlalchemy.orm import Session
from models import Item

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def generate_slug(name: str, existing_slugs: list[str] = None) -> str:
    existing = set(existing_slugs or [])
    base = re.sub(r"[^a-z0-9]+", "-", (name or "provider").lower().strip()).strip("-") or "provider"
    slug = base
    i = 2
    while slug in existing:
        slug = f"{base}-{i}"
        i += 1
    return slug


def create_item(
    db: Session,
    name: str,
    server_url: str,
    username: str,
    user_pass: str,
    languages: str,
    includes: str,
    excludes: str,
    xtream_includes: str = None,
    m3u_refresh_hours: int = 0,
    max_sessions: int = 1,
    slug: str = None,
    proxy_username: str = "iptv",
    proxy_password: str = "iptv",
):
    try:
        if not slug:
            existing = [r.slug for r in db.query(Item.slug).all() if r.slug]
            slug = generate_slug(name, existing)
        db_item = Item(
            name=name,
            server_url=server_url,
            username=username,
            user_pass=user_pass,
            languages=languages,
            includes=includes,
            excludes=excludes,
            xtream_includes=xtream_includes,
            m3u_refresh_hours=m3u_refresh_hours,
            max_sessions=max_sessions,
            slug=slug,
            proxy_username=proxy_username,
            proxy_password=proxy_password,
        )
        db.add(db_item)
        db.commit()
        db.refresh(db_item)
        logger.info("Created item id=%d name='%s' slug='%s'", db_item.id, name, slug)
        return db_item
    except Exception as e:
        logger.error("Failed to create item: %s", e)
        db.rollback()
        return None


def update_item(
    db: Session,
    item_id: int,
    name: str = None,
    server_url: str = None,
    username: str = None,
    user_pass: str = None,
    languages: str = None,
    includes: str = None,
    excludes: str = None,
    xtream_includes: str = None,
    m3u_refresh_hours: int = None,
    max_sessions: int = None,
    slug: str = None,
    proxy_username: str = None,
    proxy_password: str = None,
):
    try:
        db_item = db.query(Item).filter(Item.id == item_id).first()
        if not db_item:
            logger.warning("Item id=%d not found for update", item_id)
            return None
        fields = {
            "name": name, "server_url": server_url, "username": username,
            "user_pass": user_pass, "languages": languages, "includes": includes,
            "excludes": excludes, "xtream_includes": xtream_includes,
            "m3u_refresh_hours": m3u_refresh_hours, "max_sessions": max_sessions,
            "slug": slug, "proxy_username": proxy_username, "proxy_password": proxy_password,
        }
        for attr, val in fields.items():
            if val is not None:
                setattr(db_item, attr, val)
        db.commit()
        db.refresh(db_item)
        logger.info("Updated item id=%d", item_id)
        return db_item
    except Exception as e:
        logger.error("Failed to update item: %s", e)
        db.rollback()
        return None


def get_all_items(db: Session):
    try:
        items = db.query(Item).all()
        return items
    except Exception as e:
        logger.error("Failed to retrieve items: %s", e)
        return []


def get_item_by_slug(db: Session, slug: str):
    return db.query(Item).filter(Item.slug == slug).first()


def _build_context_for_item(item: Item, base_url: str, existing_files: set, m3u_dir: str) -> dict:
    refresh_h = item.m3u_refresh_hours
    ctx = {
        "id": item.id,
        "name": item.name,
        "server_url": item.server_url,
        "username": item.username,
        "user_pass": item.user_pass,
        "languages": item.languages,
        "includes": item.includes,
        "excludes": item.excludes,
        "xtream_includes": item.xtream_includes,
        "epg_channels": item.epg_channels,
        "m3u_refresh_hours": (
            int(refresh_h) if isinstance(refresh_h, (int, str)) and str(refresh_h).isdigit() else 0
        ),
        "has_m3u": f"xtream_playlist_{item.id}.m3u" in existing_files,
        "has_filtered": f"filtered_playlist_{item.id}.m3u" in existing_files,
        "has_epg": f"epg_{item.id}.xml" in existing_files,
        "stream_url": f"{base_url}/stream_filtered_m3u/{item.id}",
        "epg_url": f"{base_url}/epg.xml",
        "provider_status": item.provider_status,
        "provider_exp_date": item.provider_exp_date,
        "vpn_enabled": bool(item.vpn_enabled),
        "vpn_config": item.vpn_config or "",
        "vpn_username": item.vpn_username or "",
        "vpn_configured": bool(item.vpn_config and item.vpn_username and item.vpn_password),
        "max_sessions": int(item.max_sessions) if item.max_sessions is not None else 1,
        "mqtt_enabled": bool(item.mqtt_enabled),
        "mqtt_host": item.mqtt_host or "",
        "mqtt_port": int(item.mqtt_port) if item.mqtt_port is not None else 1883,
        "mqtt_username": item.mqtt_username or "",
        "mqtt_password": item.mqtt_password or "",
        "mqtt_topic_prefix": item.mqtt_topic_prefix or "",
        "mqtt_ha_discovery": bool(item.mqtt_ha_discovery),
        "mqtt_device_name": item.mqtt_device_name or "",
        "mqtt_configured": bool(item.mqtt_host),
        "slug": item.slug or "",
        "proxy_username": item.proxy_username or "iptv",
        "proxy_password": item.proxy_password or "iptv",
    }
    m3u_path = os.path.join(m3u_dir, f"xtream_playlist_{item.id}.m3u")
    try:
        ctx["m3u_last_fetched_ts"] = int(os.path.getmtime(m3u_path))
    except FileNotFoundError:
        ctx["m3u_last_fetched_ts"] = None
    return ctx


def get_item_context(db: Session, base_url: str, m3u_dir: str, item=None) -> dict | None:
    """Build context dict for a single provider. Pass item explicitly or falls back to first."""
    if item is None:
        item = db.query(Item).first()
    if not item:
        return None
    try:
        existing_files = set(os.listdir(m3u_dir))
    except Exception as e:
        logger.error("Error listing m3u_files directory: %s", e)
        existing_files = set()
    return _build_context_for_item(item, base_url, existing_files, m3u_dir)


def get_all_item_contexts(db: Session, base_url: str, m3u_dir: str) -> list[dict]:
    """Build context dicts for all providers."""
    items = db.query(Item).all()
    if not items:
        return []
    try:
        existing_files = set(os.listdir(m3u_dir))
    except Exception as e:
        logger.error("Error listing m3u_files directory: %s", e)
        existing_files = set()
    return [_build_context_for_item(it, base_url, existing_files, m3u_dir) for it in items]
