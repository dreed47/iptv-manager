from fastapi import APIRouter, Depends, HTTPException, Query, Form, Request
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from models import get_db, Item
from hdhomerun_routes import register_extra_channels
import logging
import os
import re
import fnmatch
import time
import threading
import unicodedata
import requests
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Downstream credentials — what IPTV apps use to connect TO this manager.
# These are NOT your upstream provider credentials (those live in the DB).
# ---------------------------------------------------------------------------
IPTV_USERNAME = os.getenv("IPTV_USERNAME", "iptv")
IPTV_PASSWORD = os.getenv("IPTV_PASSWORD", "iptv")

M3U_DIR = "/app/m3u_files"


def verify_credentials(username: str, password: str) -> bool:
    return username == IPTV_USERNAME and password == IPTV_PASSWORD


def _unauthorized() -> JSONResponse:
    return JSONResponse({"user_info": {"auth": 0}}, status_code=401)


# ---------------------------------------------------------------------------
# Base URL helper (mirrors hdhomerun_routes.get_advertised_base_url)
# ---------------------------------------------------------------------------
def _get_base_url() -> str:
    host = os.getenv("HDHR_ADVERTISE_HOST") or os.getenv("PUBLIC_HOST") or "127.0.0.1"
    scheme = os.getenv("HDHR_SCHEME") or "http"
    port = os.getenv("HDHR_ADVERTISE_PORT") or os.getenv("APP_PORT") or "5005"
    return f"{scheme}://{host}:{port}"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class StreamEntry:
    stream_id: int
    name: str
    logo: str
    category_name: str
    category_id: str
    url: str            # actual upstream URL — never exposed to clients
    stream_type: str    # "live" | "movie" | "series"
    guide_number: str   # only populated for live streams
    tvg_id: str         # raw tvg-id from M3U
    item_id: int = 0    # DB item ID — needed to look up provider credentials


@dataclass
class XtreamCache:
    fingerprint: tuple
    live_streams: list
    vod_streams: list
    series_streams: list
    live_categories: list
    vod_categories: list
    series_categories: list
    stream_map: dict            # stream_id (int) -> StreamEntry (all types)
    live_id_to_guide: dict      # live stream_id (int) -> guide_number (str)
    extra_channel_urls: dict    # guide_number (str) -> source_url for 24/7 channels


_cache: Optional[XtreamCache] = None
_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Stream ID helpers
# Each item gets a 100M-wide namespace; type offsets keep live/vod/series apart.
# Real-world Xtream tvg-ids are <2M so there's ample headroom.
# ---------------------------------------------------------------------------
def _make_id(item_id: int, tvg_id_str: str, type_offset: int) -> int:
    base = item_id * 100_000_000 + type_offset
    try:
        return base + int(tvg_id_str)
    except (ValueError, TypeError):
        return base + (abs(hash(tvg_id_str)) % 9_000_000)


def _live_id(item_id: int, tvg_id: str) -> int:
    return _make_id(item_id, tvg_id, 0)


def _vod_id(item_id: int, tvg_id: str) -> int:
    return _make_id(item_id, tvg_id, 10_000_000)


def _series_id(item_id: int, tvg_id: str) -> int:
    return _make_id(item_id, tvg_id, 20_000_000)


def _episode_id(item_id: int, upstream_ep_id: int) -> int:
    # Episodes use a 1B-wide namespace per item, separate from the 100M series namespace.
    return item_id * 1_000_000_000 + upstream_ep_id


# episode stream_id -> upstream provider URL (populated lazily by get_series_info calls)
_episode_cache: dict = {}
_episode_cache_lock = threading.Lock()

# vod stream_id -> correct upstream URL (populated lazily on first play via get_vod_info)
_vod_url_cache: dict = {}
_vod_url_cache_lock = threading.Lock()

_UPSTREAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


# ---------------------------------------------------------------------------
# M3U parsing helpers
# ---------------------------------------------------------------------------
def _normalize(s: str) -> str:
    s = (s or "").lower().strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.split())


def _strict_norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _normalize(s))


def _split_extinf(line: str):
    """Split #EXTINF line into (attrs_str, display_name) respecting quoted values."""
    in_quotes = False
    for idx, c in enumerate(line):
        if c == '"':
            in_quotes = not in_quotes
        elif c == ',' and not in_quotes:
            return line[:idx], line[idx + 1:].strip()
    return line, ""


def _parse_m3u_file(path: str) -> list:
    """Return a list of dicts: {tvg_id, tvg_name, logo, group_title, tvg_chno, display_name, url}."""
    entries = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return entries

    i = 1 if lines and lines[0].strip() == "#EXTM3U" else 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF") and i + 1 < len(lines):
            url = lines[i + 1].strip()
            attrs_str, display_name = _split_extinf(line)

            def _attr(key: str) -> str:
                m = re.search(rf'{key}="([^"]*)"', attrs_str)
                return m.group(1) if m else ""

            entries.append({
                "tvg_id": _attr("tvg-id"),
                "tvg_name": _attr("tvg-name"),
                "logo": _attr("tvg-logo"),
                "group_title": _attr("group-title"),
                "tvg_chno": _attr("tvg-chno"),
                "display_name": display_name,
                "url": url,
            })
            i += 2
        else:
            i += 1
    return entries


# ---------------------------------------------------------------------------
# Cache fingerprint + build
# ---------------------------------------------------------------------------
def _compute_fingerprint(items: list) -> tuple:
    parts = []
    for item in items:
        for fname in (f"xtream_playlist_{item.id}.m3u", f"filtered_playlist_{item.id}.m3u"):
            path = os.path.join(M3U_DIR, fname)
            try:
                st = os.stat(path)
                parts.append((path, st.st_mtime, st.st_size))
            except FileNotFoundError:
                parts.append((path, 0, 0))
        parts.append(("xtream_includes", item.id, getattr(item, "xtream_includes", None) or ""))
    return tuple(parts)


def _build_categories(streams: list) -> tuple:
    """Return (category_list, name_to_id_map). IDs assigned alphabetically."""
    seen = {}
    for s in streams:
        if s.category_name not in seen:
            seen[s.category_name] = str(len(seen) + 1)
    cat_list = [
        {"category_id": cid, "category_name": name, "parent_id": 0}
        for name, cid in sorted(seen.items())
    ]
    return cat_list, seen


def _build_cache(items: list, fingerprint: tuple) -> XtreamCache:
    live_streams = []
    vod_streams = []
    series_streams = []
    stream_map = {}
    live_id_to_guide = {}

    # ---- Live channels: parse filtered playlists, replicate guide-number logic ----
    used_guide_numbers: set = set()
    next_available = 1
    channels_by_name: dict = {}  # strict_norm -> (StreamEntry, explicit_bool)

    for item in items:
        filtered_path = os.path.join(M3U_DIR, f"filtered_playlist_{item.id}.m3u")
        for e in _parse_m3u_file(filtered_path):
            display = (e["tvg_name"] or e["display_name"]).replace("_", " ").strip()
            norm = _strict_norm(display)

            tvg_chno = e["tvg_chno"]
            if tvg_chno:
                guide_num = tvg_chno
                explicit = True
            else:
                while str(next_available) in used_guide_numbers:
                    next_available += 1
                guide_num = str(next_available)
                next_available += 1
                explicit = False

            sid = _live_id(item.id, e["tvg_id"])
            entry = StreamEntry(
                stream_id=sid,
                name=display,
                logo=e["logo"],
                category_name=e["group_title"] or "Live",
                category_id="",   # filled after category build
                url=e["url"],
                stream_type="live",
                guide_number=guide_num,
                tvg_id=e["tvg_id"],
                item_id=item.id,
            )

            existing = channels_by_name.get(norm)
            if existing is None:
                channels_by_name[norm] = (entry, explicit)
                used_guide_numbers.add(guide_num)
            elif not existing[1] and explicit:
                # Replace non-explicit with explicit (mirrors hdhomerun_routes logic)
                used_guide_numbers.discard(existing[0].guide_number)
                channels_by_name[norm] = (entry, explicit)
                used_guide_numbers.add(guide_num)

    # ---- VOD + Series: parse full xtream playlists ----
    for item in items:
        full_path = os.path.join(M3U_DIR, f"xtream_playlist_{item.id}.m3u")
        for e in _parse_m3u_file(full_path):
            gt = e["group_title"]
            display = (e["tvg_name"] or e["display_name"]).strip()
            if not display:
                continue

            if gt == "VOD":
                sid = _vod_id(item.id, e["tvg_id"])
                entry = StreamEntry(
                    stream_id=sid,
                    name=display,
                    logo=e["logo"],
                    category_name=gt,
                    category_id="",
                    url=e["url"],
                    stream_type="movie",
                    guide_number="",
                    tvg_id=e["tvg_id"],
                    item_id=item.id,
                )
                vod_streams.append(entry)
                stream_map[sid] = entry

            elif gt == "Series":
                sid = _series_id(item.id, e["tvg_id"])
                entry = StreamEntry(
                    stream_id=sid,
                    name=display,
                    logo=e["logo"],
                    category_name=gt,
                    category_id="",
                    url=e["url"],
                    stream_type="series",
                    guide_number="",
                    tvg_id=e["tvg_id"],
                    item_id=item.id,
                )
                series_streams.append(entry)
                stream_map[sid] = entry

    # ---- Xtream extra channels: wildcard patterns from xtream_includes, not written to filtered playlist ----
    def _xi_display_norm(s: str) -> str:
        """Lowercase + collapse non-alphanumeric to single space (preserves word boundaries)."""
        s = unicodedata.normalize('NFKD', (s or ""))
        s = ''.join(c for c in s if not unicodedata.combining(c))
        s = s.lower()
        return re.sub(r'[^a-z0-9]+', ' ', s).strip()

    def _xi_matches(pattern: str, display: str) -> bool:
        """
        *word*  → word-boundary match: 'mash' matches 'the mash show' but not 'mashing show'
        other   → fnmatch on stripped string (no spaces)
        """
        norm_d = _xi_display_norm(display)
        if pattern.startswith('*') and pattern.endswith('*') and pattern.count('*') == 2:
            inner = _xi_display_norm(pattern[1:-1])
            if not inner:
                return False
            # Build word-boundary regex: each word must match whole word, words joined by \s+
            parts = inner.split()
            regex = r'\b' + r'\s+'.join(re.escape(p) for p in parts) + r'\b'
            return bool(re.search(regex, norm_d))
        else:
            # Fallback: fnmatch on fully-stripped string
            stripped_d = re.sub(r'\s+', '', norm_d)
            stripped_p = re.sub(r'[^a-z0-9*]+', '', pattern.lower())
            return fnmatch.fnmatch(stripped_d, stripped_p)

    extra_channel_urls: dict = {}
    for item in items:
        raw_xi = getattr(item, 'xtream_includes', None) or ""
        patterns = [p.strip() for p in raw_xi.split(",") if p.strip()]
        if not patterns:
            continue
        logger.info(f"Xtream extra: item={item.id} patterns={patterns}")
        full_path = os.path.join(M3U_DIR, f"xtream_playlist_{item.id}.m3u")
        scanned = skipped_filtered = skipped_dedup = matched_count = 0
        for e in _parse_m3u_file(full_path):
            if e["group_title"] in ("VOD", "Series"):
                continue
            scanned += 1
            display = (e["tvg_name"] or e["display_name"]).replace("_", " ").strip()
            sid = _live_id(item.id, e["tvg_id"])
            if sid in stream_map:
                skipped_filtered += 1
                continue
            norm_name = _strict_norm(display)
            if norm_name in channels_by_name:
                skipped_dedup += 1
                continue
            matched = any(_xi_matches(p, display) for p in patterns)
            if not matched:
                continue
            matched_count += 1
            logger.debug(f"  Xtream extra match: '{display}'")
            guide_num = e["tvg_id"]
            entry = StreamEntry(
                stream_id=sid,
                name=display,
                logo=e["logo"],
                category_name=e["group_title"] or "Live",
                category_id="",
                url=e["url"],
                stream_type="live",
                guide_number=guide_num,
                tvg_id=e["tvg_id"],
                item_id=item.id,
            )
            channels_by_name[norm_name] = (entry, False)
            used_guide_numbers.add(guide_num)
            extra_channel_urls[guide_num] = e["url"]
        logger.info(f"Xtream extra: item={item.id} patterns={patterns} scanned={scanned} skipped_dedup={skipped_dedup} matched={matched_count}")

    # Build live_streams from the filtered channels dedup map
    live_streams = []
    live_id_to_guide = {}
    stream_map = {k: v for k, v in stream_map.items() if v.stream_type != "live"}
    for entry, _ in channels_by_name.values():
        live_streams.append(entry)
        stream_map[entry.stream_id] = entry
        live_id_to_guide[entry.stream_id] = entry.guide_number

    # ---- Assign category IDs ----
    live_cats, live_cat_map = _build_categories(live_streams)
    vod_cats, vod_cat_map = _build_categories(vod_streams)
    series_cats, series_cat_map = _build_categories(series_streams)

    for s in live_streams:
        s.category_id = live_cat_map.get(s.category_name, "1")
    for s in vod_streams:
        s.category_id = vod_cat_map.get(s.category_name, "1")
    for s in series_streams:
        s.category_id = series_cat_map.get(s.category_name, "1")

    logger.info(
        f"Xtream cache built: {len(live_streams)} live "
        f"({len(extra_channel_urls)} xtream extras), "
        f"{len(vod_streams)} VOD, {len(series_streams)} series"
    )

    return XtreamCache(
        fingerprint=fingerprint,
        live_streams=live_streams,
        vod_streams=vod_streams,
        series_streams=series_streams,
        live_categories=live_cats,
        vod_categories=vod_cats,
        series_categories=series_cats,
        stream_map=stream_map,
        live_id_to_guide=live_id_to_guide,
        extra_channel_urls=extra_channel_urls,
    )


def get_xtream_cache(db: Session) -> XtreamCache:
    global _cache
    items = db.query(Item).all()
    fingerprint = _compute_fingerprint(items)
    with _cache_lock:
        if _cache and _cache.fingerprint == fingerprint:
            return _cache
        _cache = _build_cache(items, fingerprint)
        if _cache.extra_channel_urls:
            register_extra_channels(_cache.extra_channel_urls)
        return _cache


# ---------------------------------------------------------------------------
# JSON serialization helpers
# ---------------------------------------------------------------------------
def _stream_to_json(entry: StreamEntry) -> dict:
    if entry.stream_type == "live":
        return {
            "num": entry.stream_id,
            "name": entry.name,
            "stream_type": "live",
            "stream_id": entry.stream_id,
            "stream_icon": entry.logo,
            "epg_channel_id": entry.tvg_id,
            "added": "0",
            "category_id": entry.category_id,
            "category_name": entry.category_name,
            "custom_sid": "",
            "tv_archive": 0,
            "direct_source": "",
        }
    if entry.stream_type == "movie":
        return {
            "num": entry.stream_id,
            "name": entry.name,
            "stream_type": "movie",
            "stream_id": entry.stream_id,
            "stream_icon": entry.logo,
            "rating": "0",
            "added": "0",
            "category_id": entry.category_id,
            "category_name": entry.category_name,
            "container_extension": "mp4",
            "custom_sid": "",
            "direct_source": "",
        }
    # series
    return {
        "num": entry.stream_id,
        "name": entry.name,
        "series_id": entry.stream_id,
        "stream_icon": entry.logo,
        "rating": "0",
        "added": "0",
        "category_id": entry.category_id,
        "category_name": entry.category_name,
        "cover": entry.logo,
        "plot": "",
        "cast": "",
        "director": "",
        "genre": "",
        "release_date": "",
        "last_modified": "0",
        "episode_run_time": "0",
        "youtube_trailer": "",
    }


def _auth_response(base_url: str) -> dict:
    # Parse host and port from base_url for server_info
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        srv_host = parsed.hostname or "127.0.0.1"
        srv_port = str(parsed.port or 5005)
        srv_scheme = parsed.scheme or "http"
    except Exception:
        srv_host, srv_port, srv_scheme = "127.0.0.1", "5005", "http"

    return {
        "user_info": {
            "username": IPTV_USERNAME,
            "password": IPTV_PASSWORD,
            "message": "",
            "auth": 1,
            "status": "Active",
            "exp_date": "9999999999",
            "is_trial": "0",
            "active_cons": "0",
            "created_at": "0",
            "max_connections": "10",
            "allowed_output_formats": ["ts", "m3u8", "rtmp"],
        },
        "server_info": {
            "url": srv_host,
            "port": srv_port,
            "https_port": "443",
            "server_protocol": srv_scheme,
            "rtmp_port": "1935",
            "timezone": "UTC",
            "timestamp_now": int(time.time()),
            "time_now": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


def _vod_info_response(entry: StreamEntry) -> dict:
    return {
        "info": {
            "name": entry.name,
            "o_name": entry.name,
            "cover_big": entry.logo,
            "movie_image": entry.logo,
            "release_date": "",
            "episode_run_time": "",
            "rating": "0",
            "description": "",
            "genre": "",
            "cast": "",
            "director": "",
            "youtube_trailer": "",
        },
        "movie_data": {
            "stream_id": entry.stream_id,
            "name": entry.name,
            "added": "0",
            "category_id": entry.category_id,
            "container_extension": "mp4",
            "custom_sid": "",
            "direct_source": "",
        },
    }


def _fetch_vod_url(entry: StreamEntry, db: Session) -> Optional[str]:
    """Call get_vod_info upstream to get the correct container extension, cache and return the URL."""
    item = db.query(Item).filter(Item.id == entry.item_id).first()
    if not item:
        return None
    base = item.server_url.rstrip("/")
    try:
        resp = requests.get(
            f"{base}/player_api.php",
            params={
                "username": item.username,
                "password": item.user_pass,
                "action": "get_vod_info",
                "vod_id": entry.tvg_id,
            },
            headers={**_UPSTREAM_HEADERS, "Referer": base},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning(f"Upstream get_vod_info failed for vod {entry.tvg_id}: {exc}")
        return None

    ext = (data.get("movie_data") or {}).get("container_extension") or "mp4"
    url = f"{base}/movie/{item.username}/{item.user_pass}/{entry.tvg_id}.{ext}"
    with _vod_url_cache_lock:
        _vod_url_cache[entry.stream_id] = url
    logger.info(f"VOD {entry.stream_id} resolved extension: .{ext} → {url}")
    return url


def _fetch_series_info(entry: StreamEntry, db: Session) -> dict:
    """Call the upstream Xtream API for series info, cache episode URLs, return API response."""
    item = db.query(Item).filter(Item.id == entry.item_id).first()
    if not item:
        return _series_info_fallback(entry)

    try:
        base = item.server_url.rstrip("/")
        resp = requests.get(
            f"{base}/player_api.php",
            params={
                "username": item.username,
                "password": item.user_pass,
                "action": "get_series_info",
                "series_id": entry.tvg_id,
            },
            headers={**_UPSTREAM_HEADERS, "Referer": base},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning(f"Upstream get_series_info failed for series {entry.tvg_id}: {exc}")
        return _series_info_fallback(entry)

    # Rewrite episode IDs to our namespaced local IDs and cache their upstream URLs
    episodes_out = {}
    for season_num, episodes in data.get("episodes", {}).items():
        season_list = []
        for ep in episodes:
            try:
                upstream_ep_id = int(ep.get("id", 0))
            except (ValueError, TypeError):
                continue
            ext = ep.get("container_extension", "mp4")
            local_id = _episode_id(entry.item_id, upstream_ep_id)
            upstream_url = (
                f"{base}/series/{item.username}/{item.user_pass}/{upstream_ep_id}.{ext}"
            )
            with _episode_cache_lock:
                _episode_cache[local_id] = upstream_url
            ep_out = dict(ep)
            ep_out["id"] = str(local_id)
            ep_out["direct_source"] = ""
            season_list.append(ep_out)
        if season_list:
            episodes_out[season_num] = season_list

    info = data.get("info", {})
    info.setdefault("name", entry.name)
    info.setdefault("cover", entry.logo)
    return {"info": info, "episodes": episodes_out}


def _series_info_fallback(entry: StreamEntry) -> dict:
    """Minimal series info when the upstream API is unavailable."""
    return {
        "info": {
            "name": entry.name,
            "cover": entry.logo,
            "plot": "",
            "cast": "",
            "director": "",
            "genre": "",
            "release_date": "",
            "rating": "0",
            "backdrop_path": [],
            "youtube_trailer": "",
            "episode_run_time": "0",
            "category_id": entry.category_id,
        },
        "episodes": {},
    }


# ---------------------------------------------------------------------------
# Xtream API endpoint  (GET and POST)
# ---------------------------------------------------------------------------
async def _handle_player_api(
    username: str,
    password: str,
    action: Optional[str],
    vod_id: Optional[str],
    series_id: Optional[str],
    db: Session,
) -> Response:
    if not verify_credentials(username, password):
        return _unauthorized()

    base_url = _get_base_url()
    cache = get_xtream_cache(db)

    if action is None:
        return JSONResponse(_auth_response(base_url))

    if action == "get_live_categories":
        return JSONResponse(cache.live_categories)
    if action == "get_vod_categories":
        return JSONResponse(cache.vod_categories)
    if action == "get_series_categories":
        return JSONResponse(cache.series_categories)

    if action == "get_live_streams":
        return JSONResponse([_stream_to_json(s) for s in cache.live_streams])
    if action == "get_vod_streams":
        return JSONResponse([_stream_to_json(s) for s in cache.vod_streams])
    if action == "get_series":
        return JSONResponse([_stream_to_json(s) for s in cache.series_streams])

    if action == "get_vod_info" and vod_id:
        try:
            entry = cache.stream_map.get(int(vod_id))
        except (ValueError, TypeError):
            entry = None
        if not entry or entry.stream_type != "movie":
            return JSONResponse({"info": {}, "movie_data": {}})
        return JSONResponse(_vod_info_response(entry))

    if action == "get_series_info" and series_id:
        try:
            entry = cache.stream_map.get(int(series_id))
        except (ValueError, TypeError):
            entry = None
        if not entry or entry.stream_type != "series":
            return JSONResponse({"info": {}, "episodes": {}})
        return JSONResponse(_fetch_series_info(entry, db))

    return JSONResponse({"error": "Unknown action"}, status_code=400)


@router.get("/player_api.php")
async def player_api_get(
    username: str = Query(...),
    password: str = Query(...),
    action: Optional[str] = Query(None),
    vod_id: Optional[str] = Query(None),
    series_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    return await _handle_player_api(username, password, action, vod_id, series_id, db)


@router.post("/player_api.php")
async def player_api_post(
    username: str = Form(...),
    password: str = Form(...),
    action: Optional[str] = Form(None),
    vod_id: Optional[str] = Form(None),
    series_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    return await _handle_player_api(username, password, action, vod_id, series_id, db)


# ---------------------------------------------------------------------------
# M3U playlist endpoints
# ---------------------------------------------------------------------------
def _m3u_generator(cache: XtreamCache, base_url: str, username: str, password: str):
    yield "#EXTM3U\n"
    for s in cache.live_streams:
        url = f"{base_url}/live/{username}/{password}/{s.stream_id}.ts"
        yield (
            f'#EXTINF:-1 tvg-id="{s.tvg_id}" tvg-name="{s.name}" '
            f'tvg-logo="{s.logo}" group-title="{s.category_name}",{s.name}\n'
            f"{url}\n"
        )
    for s in cache.vod_streams:
        url = f"{base_url}/movie/{username}/{password}/{s.stream_id}.mp4"
        yield (
            f'#EXTINF:-1 tvg-id="{s.tvg_id}" tvg-name="{s.name}" '
            f'tvg-logo="{s.logo}" group-title="{s.category_name}",{s.name}\n'
            f"{url}\n"
        )
    for s in cache.series_streams:
        url = f"{base_url}/series/{username}/{password}/{s.stream_id}.m3u8"
        yield (
            f'#EXTINF:-1 tvg-id="{s.tvg_id}" tvg-name="{s.name}" '
            f'tvg-logo="{s.logo}" group-title="{s.category_name}",{s.name}\n'
            f"{url}\n"
        )


@router.get("/get.php")
async def get_m3u_xtream(
    username: str = Query(...),
    password: str = Query(...),
    type: str = Query("m3u_plus"),
    output: str = Query("ts"),
    db: Session = Depends(get_db),
):
    if not verify_credentials(username, password):
        return Response("Unauthorized", status_code=401)
    base_url = _get_base_url()
    cache = get_xtream_cache(db)
    return StreamingResponse(
        _m3u_generator(cache, base_url, username, password),
        media_type="application/x-mpegurl",
        headers={"Content-Disposition": 'attachment; filename="playlist.m3u"'},
    )


@router.get("/iptv/playlist.m3u")
async def get_m3u_simple(
    username: Optional[str] = Query(None),
    password: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    if username and password and not verify_credentials(username, password):
        return Response("Unauthorized", status_code=401)
    u = username or IPTV_USERNAME
    p = password or IPTV_PASSWORD
    base_url = _get_base_url()
    cache = get_xtream_cache(db)
    return StreamingResponse(
        _m3u_generator(cache, base_url, u, p),
        media_type="application/x-mpegurl",
        headers={"Content-Disposition": 'attachment; filename="playlist.m3u"'},
    )


# ---------------------------------------------------------------------------
# EPG redirect (some apps call /xmltv.php)
# ---------------------------------------------------------------------------
@router.get("/xmltv.php")
async def xmltv_redirect(
    username: str = Query(...),
    password: str = Query(...),
):
    if not verify_credentials(username, password):
        return _unauthorized()
    return RedirectResponse(url="/epg.xml", status_code=302)


# ---------------------------------------------------------------------------
# Stream proxy endpoints
# ---------------------------------------------------------------------------

@router.get("/live/{username}/{password}/{stream_id_ext:path}")
async def proxy_live(
    username: str,
    password: str,
    stream_id_ext: str,
    db: Session = Depends(get_db),
):
    if not verify_credentials(username, password):
        raise HTTPException(status_code=401, detail="Unauthorized")

    stream_id_str = stream_id_ext.split(".")[0]
    try:
        stream_id = int(stream_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid stream ID")

    cache = get_xtream_cache(db)
    guide_number = cache.live_id_to_guide.get(stream_id)
    if not guide_number:
        raise HTTPException(status_code=404, detail=f"Live stream {stream_id} not found")

    return RedirectResponse(url=f"/auto/v{guide_number}", status_code=307)


def _proxy_finite_stream(source_url: str, request: Request, media_type: str):
    """Proxy a finite stream (VOD/series) with Range support for seeking."""
    chunk_size = int(os.getenv("STREAM_CHUNK_KB", "64")) * 1024
    proxy_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
    }
    range_header = request.headers.get("range")
    # Always send a Range header — many providers require it for VOD/series.
    # Use the client's Range if present, otherwise request the full file.
    proxy_headers["Range"] = range_header or "bytes=0-"

    # Open the upstream connection before committing to a StreamingResponse so
    # we can return a proper HTTP error if the upstream rejects the request.
    # NOTE: We return a JSONResponse on error rather than raising HTTPException
    # to avoid triggering the get_db generator cleanup bug (its bare except
    # catches HTTPException and attempts a second yield, causing RuntimeError).
    try:
        resp = requests.get(
            source_url,
            headers=proxy_headers,
            stream=True,
            timeout=(10, 60),
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        upstream_status = exc.response.status_code if exc.response is not None else 0
        reason = exc.response.reason if exc.response is not None else "unknown"
        body_preview = ""
        if exc.response is not None:
            try:
                body_preview = exc.response.text[:200]
            except Exception:
                pass
        logger.warning(
            f"Upstream {upstream_status} for {source_url}: {reason} | body: {body_preview!r}"
        )
        return JSONResponse(
            {"error": f"Upstream error {upstream_status}: {reason}"},
            status_code=502,
        )
    except Exception as exc:
        logger.warning(f"Upstream connection failed for {source_url}: {exc}")
        return JSONResponse({"error": f"Upstream connection failed: {exc}"}, status_code=502)

    # Forward headers the client needs to seek and buffer correctly
    forward_headers = {
        "Cache-Control": "no-cache",
        "Accept-Ranges": "bytes",
    }
    for hdr in ("Content-Range", "Content-Length", "Content-Type"):
        val = resp.headers.get(hdr)
        if val:
            forward_headers[hdr] = val

    logger.info(
        f"Proxying {source_url} → HTTP {resp.status_code} "
        f"content-type={forward_headers.get('Content-Type', 'unknown')} "
        f"content-length={forward_headers.get('Content-Length', 'unknown')} "
        f"content-range={forward_headers.get('Content-Range', 'none')}"
    )

    def generate():
        try:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if chunk:
                    yield chunk
        except Exception as exc:
            logger.error(f"Stream error mid-transfer for {source_url}: {exc}")
        finally:
            resp.close()

    return StreamingResponse(
        generate(),
        status_code=resp.status_code,
        media_type=media_type,
        headers=forward_headers,
    )


@router.get("/movie/{username}/{password}/{stream_id_ext:path}")
async def proxy_vod(
    username: str,
    password: str,
    stream_id_ext: str,
    request: Request,
    db: Session = Depends(get_db),
):
    if not verify_credentials(username, password):
        raise HTTPException(status_code=401, detail="Unauthorized")

    stream_id_str = stream_id_ext.split(".")[0]
    try:
        stream_id = int(stream_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid stream ID")

    cache = get_xtream_cache(db)
    entry = cache.stream_map.get(stream_id)
    if not entry or entry.stream_type != "movie":
        raise HTTPException(status_code=404, detail=f"VOD stream {stream_id} not found")

    # Check if we already have the correct URL (resolved extension from a prior get_vod_info call)
    with _vod_url_cache_lock:
        resolved_url = _vod_url_cache.get(stream_id)

    if resolved_url is None:
        # First play: fetch correct extension from upstream, fall back to M3U URL on failure
        resolved_url = _fetch_vod_url(entry, db) or entry.url

    result = _proxy_finite_stream(resolved_url, request, "video/mp4")

    # If the resolved URL also fails (551 etc.), invalidate cache so next request retries
    if isinstance(result, JSONResponse) and result.status_code == 502:
        with _vod_url_cache_lock:
            _vod_url_cache.pop(stream_id, None)

    return result


@router.get("/series/{username}/{password}/{stream_id_ext:path}")
async def proxy_series(
    username: str,
    password: str,
    stream_id_ext: str,
    request: Request,
    db: Session = Depends(get_db),
):
    if not verify_credentials(username, password):
        raise HTTPException(status_code=401, detail="Unauthorized")

    stream_id_str = stream_id_ext.split(".")[0]
    try:
        stream_id = int(stream_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid stream ID")

    # Episode cache is populated when get_series_info is called; check it first
    with _episode_cache_lock:
        upstream_url = _episode_cache.get(stream_id)
    if upstream_url:
        logger.info(f"Series episode {stream_id} → {upstream_url}")
        return _proxy_finite_stream(upstream_url, request, "video/mp4")

    # Episode not in cache — get_series_info hasn't been called yet for this series
    logger.warning(f"Series episode {stream_id} not in episode cache (get_series_info not yet called?)")
    cache = get_xtream_cache(db)
    entry = cache.stream_map.get(stream_id)
    if not entry or entry.stream_type != "series":
        logger.warning(f"Series stream {stream_id} not found in stream_map either")
        return JSONResponse({"error": f"Series stream {stream_id} not found"}, status_code=404)

    return _proxy_finite_stream(entry.url, request, "application/vnd.apple.mpegurl")
