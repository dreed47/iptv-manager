import re
import json
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


def _count_extinf(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for line in f if line.startswith("#EXTINF"))
    except Exception:
        return 0


def _count_epg_channels(path: str) -> int:
    """Count <channel  occurrences using chunked reads — avoids loading large EPG files into memory."""
    try:
        needle = b"<channel "
        nlen = len(needle)
        count = 0
        buf = b""
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                combined = buf + chunk
                count += combined.count(needle)
                buf = combined[-(nlen - 1):]
        return count
    except Exception:
        return 0


def _get_file_mtime(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except FileNotFoundError:
        return None


def _load_count_cache(cache_path: str) -> dict:
    try:
        with open(cache_path) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_count_cache(cache_path: str, data: dict) -> None:
    try:
        with open(cache_path, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _cached_count(path: str, count_fn, cache: dict, count_key: str, mtime_key: str) -> tuple[int, bool]:
    """Return (count, cache_was_updated). Uses cached value if mtime unchanged."""
    mtime = _get_file_mtime(path)
    if mtime is not None and cache.get(mtime_key) == mtime:
        return cache.get(count_key, 0), False
    count = count_fn(path) if mtime is not None else 0
    cache[mtime_key] = mtime
    cache[count_key] = count
    return count, True


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

    cache_path = os.path.join(m3u_dir, f"counts_{item.id}.json")
    cache = _load_count_cache(cache_path)
    dirty = False

    filtered_path = os.path.join(m3u_dir, f"filtered_playlist_{item.id}.m3u")
    epg_path = os.path.join(m3u_dir, f"epg_{item.id}.xml")

    m3u_count, d = _cached_count(m3u_path, _count_extinf, cache, "m3u_count", "m3u_mtime")
    dirty = dirty or d
    filtered_count, d = _cached_count(filtered_path, _count_extinf, cache, "filtered_count", "filtered_mtime")
    dirty = dirty or d
    epg_count, d = _cached_count(epg_path, _count_epg_channels, cache, "epg_count", "epg_mtime")
    dirty = dirty or d

    if dirty:
        _save_count_cache(cache_path, cache)

    ctx["m3u_count"] = m3u_count if ctx["has_m3u"] else 0
    ctx["filtered_count"] = filtered_count if ctx["has_filtered"] else 0
    ctx["epg_count"] = epg_count if ctx["has_epg"] else 0
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


def get_generated_epg_count(m3u_dir: str) -> int:
    """Return channel count from generated_epg.xml, using a sidecar cache keyed by mtime."""
    epg_path = os.path.join(m3u_dir, "generated_epg.xml")
    cache_path = os.path.join(m3u_dir, "counts_generated.json")
    cache = _load_count_cache(cache_path)
    count, dirty = _cached_count(epg_path, _count_epg_channels, cache, "epg_count", "epg_mtime")
    if dirty:
        _save_count_cache(cache_path, cache)
    return count


def write_count_to_cache(m3u_dir: str, cache_name: str, count_key: str, count: int, file_path: str) -> None:
    """Persist a pre-computed count to the sidecar cache immediately after a file is written.

    NOTE: The UI uses `_cached_count(..., mtime_key=...)` with mtime keys of:
      - m3u_mtime
      - filtered_mtime
      - epg_mtime

    Older versions wrote keys like "m3u_count_mtime", which caused the UI to
    re-count large files on every page load (slow / "hung" pages).

    cache_name is the item id (as string) or 'generated' for the merged EPG.
    """
    cache_path = os.path.join(m3u_dir, f"counts_{cache_name}.json")
    cache = _load_count_cache(cache_path)

    # Keep mtime key names consistent with _cached_count callers.
    if count_key == "m3u_count":
        mtime_key = "m3u_mtime"
    elif count_key == "filtered_count":
        mtime_key = "filtered_mtime"
    elif count_key == "epg_count":
        mtime_key = "epg_mtime"
    else:
        mtime_key = f"{count_key}_mtime"

    cache[mtime_key] = _get_file_mtime(file_path)
    cache[count_key] = count
    _save_count_cache(cache_path, cache)
