from fastapi import APIRouter, Depends, HTTPException, Query, Form, Request
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse, Response, FileResponse
from sqlalchemy.orm import Session
from models import get_db, Item
from hdhomerun_routes import register_extra_channels, _active_streams, _active_streams_lock, _SESSION_STALE_SECONDS, get_active_stream_count, is_ip_blocked, auto_replace_ip_session, KILL_BLOCK_SECONDS
import asyncio
import config
import gc
import hashlib
import json
import logging
import os
import re
import fnmatch
import sqlite3
import time
import threading
import uuid
import unicodedata
import requests
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)
router = APIRouter()

M3U_DIR = config.M3U_DIR


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def verify_credentials(username: str, password: str, item: Item) -> bool:
    expected_user = item.proxy_username or "iptv"
    expected_pass = item.proxy_password or "iptv"
    return username == expected_user and password == expected_pass


def _unauthorized() -> JSONResponse:
    return JSONResponse({"user_info": {"auth": 0}}, status_code=401)


def _get_base_url() -> str:
    return config.ADVERTISED_BASE_URL


async def get_item_for_slug(provider_slug: str, db: Session = Depends(get_db)) -> Item:
    item = db.query(Item).filter(Item.slug == provider_slug).first()
    if not item:
        all_items = db.query(Item).all()
        available = [f"/{i.slug}/" for i in all_items if i.slug]
        raise HTTPException(
            status_code=404,
            detail={"error": "Provider not found", "available_providers": available},
        )
    return item


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class StreamEntry:
    stream_id: int
    name: str
    logo: str
    category_name: str
    category_id: str
    url: str            # actual upstream URL — never exposed to clients
    stream_type: str    # "live" only (VOD/series are in SQLite)
    guide_number: str
    tvg_id: str
    item_id: int = 0


@dataclass(slots=True)
class XtreamCache:
    fingerprint: tuple
    live_streams: list           # StreamEntry list — live channels only (~100–200 entries)
    live_stream_map: dict        # stream_id -> StreamEntry (live only)
    live_categories: list
    vod_categories: list
    series_categories: list
    live_id_to_guide: dict
    extra_channel_urls: dict
    extra_channel_names: dict
    db_path: str                 # SQLite catalog (VOD + Series rows)
    vod_json_path: str           # pre-built JSON array for get_vod_streams
    series_json_path: str        # pre-built JSON array for get_series
    item_id: int                 # primary provider item id


# Per-item cache: item_id -> XtreamCache
_cache: dict[int, XtreamCache] = {}
_cache_lock = asyncio.Lock()

# Tracks last build failure time per item_id to prevent tight rebuild loops
_build_failures: dict[int, float] = {}
_BUILD_RETRY_COOLDOWN = 60.0  # seconds before retrying after a failed build


# ---------------------------------------------------------------------------
# Stream ID helpers
# ---------------------------------------------------------------------------

def _make_id(item_id: int, tvg_id_str: str, type_offset: int) -> int:
    base = item_id * 100_000_000 + type_offset
    try:
        return base + int(tvg_id_str)
    except (ValueError, TypeError):
        return base + (int(hashlib.md5(tvg_id_str.encode()).hexdigest(), 16) % 9_000_000)


def _live_id(item_id: int, tvg_id: str) -> int:
    return _make_id(item_id, tvg_id, 0)


def _vod_id(item_id: int, tvg_id: str) -> int:
    return _make_id(item_id, tvg_id, 10_000_000)


def _series_id(item_id: int, tvg_id: str) -> int:
    return _make_id(item_id, tvg_id, 20_000_000)


def _episode_id(item_id: int, upstream_ep_id: int) -> int:
    return item_id * 1_000_000_000 + upstream_ep_id


# ---------------------------------------------------------------------------
# Lazy URL caches (populated on first play)
# ---------------------------------------------------------------------------

_episode_cache: dict = {}
_episode_cache_lock = threading.Lock()

_vod_url_cache: dict = {}
_vod_url_cache_lock = threading.Lock()

_CACHE_MAX = 10_000

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


_ATTR_RE = re.compile(r'(tvg-id|tvg-name|tvg-logo|group-title|tvg-chno)="([^"]*)"')


def _stream_m3u_file(path: str):
    """Stream M3U entries one at a time — O(1) memory regardless of file size."""
    try:
        f = open(path, "r", encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        return
    try:
        prev_extinf = None
        first = True
        for raw in f:
            line = raw.rstrip("\n")
            if first:
                first = False
                if line.strip() == "#EXTM3U":
                    continue
            if line.startswith("#EXTINF"):
                prev_extinf = line
            elif prev_extinf is not None:
                url = line.strip()
                attrs_str, display_name = _split_extinf(prev_extinf)
                attrs = dict(_ATTR_RE.findall(attrs_str))
                yield {
                    "tvg_id": attrs.get("tvg-id", ""),
                    "tvg_name": attrs.get("tvg-name", ""),
                    "logo": attrs.get("tvg-logo", ""),
                    "group_title": attrs.get("group-title", ""),
                    "tvg_chno": attrs.get("tvg-chno", ""),
                    "display_name": display_name,
                    "url": url,
                }
                prev_extinf = None
    finally:
        f.close()


def _xi_display_norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# ---------------------------------------------------------------------------
# SQLite catalog helpers
# ---------------------------------------------------------------------------

_CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
    stream_id   INTEGER PRIMARY KEY,
    tvg_id      TEXT    NOT NULL DEFAULT '',
    name        TEXT    NOT NULL DEFAULT '',
    logo        TEXT    NOT NULL DEFAULT '',
    category_name TEXT  NOT NULL DEFAULT '',
    category_id TEXT    NOT NULL DEFAULT '1',
    url         TEXT    NOT NULL DEFAULT '',
    stream_type TEXT    NOT NULL,
    item_id     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_item_type ON streams(item_id, stream_type);
"""


@contextmanager
def _catalog_conn(db_path: str):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _catalog_lookup(db_path: str, stream_id: int):
    with _catalog_conn(db_path) as conn:
        return conn.execute(
            "SELECT * FROM streams WHERE stream_id=?", (stream_id,)
        ).fetchone()


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


def _build_cache(items: list, fingerprint: tuple) -> XtreamCache:  # noqa: C901
    primary_item_id = items[0].id
    db_key = "_".join(str(i.id) for i in items)
    db_path = os.path.join(M3U_DIR, f"catalog_{db_key}.db")
    vod_json_path = os.path.join(M3U_DIR, f"vod_cache_{db_key}.json")
    series_json_path = os.path.join(M3U_DIR, f"series_cache_{db_key}.json")

    # ---- Live channels: stream filtered playlists (small files) ----
    used_guide_numbers: set = set()
    next_available = 1
    channels_by_name: dict = {}

    for item in items:
        filtered_path = os.path.join(M3U_DIR, f"filtered_playlist_{item.id}.m3u")
        for e in _stream_m3u_file(filtered_path):
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
                category_id="",
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
                used_guide_numbers.discard(existing[0].guide_number)
                channels_by_name[norm] = (entry, explicit)
                used_guide_numbers.add(guide_num)

    # ---- Pre-build extra channel pattern matchers (one per item) ----
    def _make_matcher(compiled_patterns):
        def _matches(display: str) -> bool:
            norm_d = _xi_display_norm(display)
            for kind, matcher in compiled_patterns:
                if kind == "word":
                    if matcher.search(norm_d):
                        return True
                else:
                    if fnmatch.fnmatch(re.sub(r"\s+", "", norm_d), matcher):
                        return True
            return False
        return _matches

    item_matchers = []  # list of (item, has_patterns, match_fn)
    for item in items:
        raw_xi = getattr(item, "xtream_includes", None) or ""
        patterns = [p.strip() for p in raw_xi.split(",") if p.strip()]
        if not patterns:
            item_matchers.append((item, False, None))
            continue
        compiled = []
        for p in patterns:
            if p.startswith("*") and p.endswith("*") and p.count("*") == 2:
                inner = _xi_display_norm(p[1:-1])
                if inner:
                    words = inner.split()
                    rx = re.compile(r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b")
                    compiled.append(("word", rx))
            else:
                compiled.append(("fnmatch", re.sub(r"[^a-z0-9*]+", "", p.lower())))
        item_matchers.append((item, bool(compiled), _make_matcher(compiled)))

    # ---- Single-pass streaming: VOD + Series → SQLite + JSON; live → extra channels ----
    vod_tmp = vod_json_path + ".tmp"
    series_tmp = series_json_path + ".tmp"

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_CATALOG_SCHEMA)
    conn.execute("DELETE FROM streams")

    batch: list = []
    BATCH_SIZE = 2000

    def _flush():
        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO streams "
                "(stream_id,tvg_id,name,logo,category_name,category_id,url,stream_type,item_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                batch,
            )
            batch.clear()

    extra_channel_urls: dict = {}
    extra_channel_names: dict = {}

    with open(vod_tmp, "w", encoding="utf-8") as vod_f, \
         open(series_tmp, "w", encoding="utf-8") as series_f:
        vod_f.write("[")
        series_f.write("[")
        vod_first = series_first = True

        for item, has_patterns, _matches in item_matchers:
            full_path = os.path.join(M3U_DIR, f"xtream_playlist_{item.id}.m3u")

            for e in _stream_m3u_file(full_path):
                gt = e["group_title"]
                display = (e["tvg_name"] or e["display_name"]).strip()
                if not display:
                    continue

                if gt == "VOD":
                    sid = _vod_id(item.id, e["tvg_id"])
                    batch.append((sid, e["tvg_id"], display, e["logo"] or "",
                                  "VOD", "1", e["url"] or "", "movie", item.id))
                    if len(batch) >= BATCH_SIZE:
                        _flush()
                    obj = {
                        "num": sid, "name": display, "stream_type": "movie",
                        "stream_id": sid, "stream_icon": e["logo"] or "",
                        "rating": "0", "added": "0",
                        "category_id": "1", "category_name": "VOD",
                        "container_extension": "mp4", "custom_sid": "",
                        "direct_source": "",
                    }
                    if not vod_first:
                        vod_f.write(",")
                    vod_f.write(json.dumps(obj, ensure_ascii=False))
                    vod_first = False

                elif gt == "Series":
                    sid = _series_id(item.id, e["tvg_id"])
                    batch.append((sid, e["tvg_id"], display, e["logo"] or "",
                                  "Series", "1", e["url"] or "", "series", item.id))
                    if len(batch) >= BATCH_SIZE:
                        _flush()
                    obj = {
                        "num": sid, "name": display, "series_id": sid,
                        "stream_icon": e["logo"] or "",
                        "rating": "0", "added": "0",
                        "category_id": "1", "category_name": "Series",
                        "cover": e["logo"] or "", "plot": "", "cast": "",
                        "director": "", "genre": "", "release_date": "",
                        "last_modified": "0", "episode_run_time": "0",
                        "youtube_trailer": "",
                    }
                    if not series_first:
                        series_f.write(",")
                    series_f.write(json.dumps(obj, ensure_ascii=False))
                    series_first = False

                elif has_patterns:
                    # Extra channel candidate — live channel not in filtered playlist
                    disp = display.replace("_", " ").strip()
                    norm_name = _strict_norm(disp)
                    if norm_name in channels_by_name:
                        continue
                    if not _matches(disp):
                        continue
                    sid = _live_id(item.id, e["tvg_id"])
                    guide_num = e["tvg_id"]
                    entry = StreamEntry(
                        stream_id=sid,
                        name=disp,
                        logo=e["logo"],
                        category_name=gt or "Live",
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
                    extra_channel_names[guide_num] = disp

        vod_f.write("]")
        series_f.write("]")

    _flush()
    conn.commit()
    conn.close()

    os.replace(vod_tmp, vod_json_path)
    os.replace(series_tmp, series_json_path)

    # ---- Assemble live streams ----
    live_streams = []
    live_stream_map = {}
    live_id_to_guide = {}
    for entry, _ in channels_by_name.values():
        live_streams.append(entry)
        live_stream_map[entry.stream_id] = entry
        live_id_to_guide[entry.stream_id] = entry.guide_number

    live_cats, live_cat_map = _build_categories(live_streams)
    for s in live_streams:
        s.category_id = live_cat_map.get(s.category_name, "1")

    vod_cats = [{"category_id": "1", "category_name": "VOD", "parent_id": 0}]
    series_cats = [{"category_id": "1", "category_name": "Series", "parent_id": 0}]

    gc.collect()

    try:
        with _catalog_conn(db_path) as conn:
            vod_count = conn.execute(
                "SELECT COUNT(*) FROM streams WHERE stream_type='movie'"
            ).fetchone()[0]
            series_count = conn.execute(
                "SELECT COUNT(*) FROM streams WHERE stream_type='series'"
            ).fetchone()[0]
    except Exception:
        vod_count = series_count = 0

    provider_label = ", ".join(f"'{i.name}'" for i in items)
    logger.info(
        f"Xtream cache built [{provider_label}]: {len(live_streams)} live, "
        f"{vod_count} VOD, {series_count} series → {db_path}"
    )

    return XtreamCache(
        fingerprint=fingerprint,
        live_streams=live_streams,
        live_stream_map=live_stream_map,
        live_categories=live_cats,
        vod_categories=vod_cats,
        series_categories=series_cats,
        live_id_to_guide=live_id_to_guide,
        extra_channel_urls=extra_channel_urls,
        extra_channel_names=extra_channel_names,
        db_path=db_path,
        vod_json_path=vod_json_path,
        series_json_path=series_json_path,
        item_id=primary_item_id,
    )


async def get_xtream_cache(db: Session, item: Item) -> XtreamCache:
    global _cache
    fingerprint = _compute_fingerprint([item])
    async with _cache_lock:
        cached = _cache.get(item.id)
        if cached and cached.fingerprint == fingerprint:
            # Verify catalog files still exist (could be deleted externally)
            if (os.path.exists(cached.db_path)
                    and os.path.exists(cached.vod_json_path)
                    and os.path.exists(cached.series_json_path)):
                return cached

        # Enforce cooldown after build failures to prevent tight rebuild loops
        last_fail = _build_failures.get(item.id, 0.0)
        if time.time() - last_fail < _BUILD_RETRY_COOLDOWN:
            logger.warning(
                f"Xtream cache build cooldown active for item {item.id} "
                f"({_BUILD_RETRY_COOLDOWN - (time.time() - last_fail):.0f}s remaining) — "
                f"returning {'stale cache' if cached else 'error'}"
            )
            if cached:
                return cached
            raise HTTPException(status_code=503, detail="Cache unavailable — build failed recently, try again shortly")

        try:
            new_cache = await asyncio.to_thread(_build_cache, [item], fingerprint)
        except Exception as exc:
            _build_failures[item.id] = time.time()
            logger.error(f"Xtream cache build failed for item {item.id}: {exc}", exc_info=True)
            if cached:
                logger.warning(f"Xtream cache: returning stale cache for item {item.id} after build failure")
                return cached
            raise HTTPException(status_code=503, detail=f"Cache build failed: {exc}")

        _build_failures.pop(item.id, None)
        _cache[item.id] = new_cache
        if new_cache.extra_channel_urls:
            register_extra_channels(new_cache.extra_channel_urls, new_cache.extra_channel_names)
        return new_cache


# ---------------------------------------------------------------------------
# JSON serialization helpers
# ---------------------------------------------------------------------------

def _stream_to_json(entry: StreamEntry) -> dict:
    """Used for live streams only."""
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


def _auth_response(base_url: str, item: Item) -> dict:
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
            "username": item.proxy_username or "iptv",
            "password": item.proxy_password or "iptv",
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


def _vod_info_response_from_row(row) -> dict:
    return {
        "info": {
            "name": row["name"],
            "o_name": row["name"],
            "cover_big": row["logo"],
            "movie_image": row["logo"],
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
            "stream_id": row["stream_id"],
            "name": row["name"],
            "added": "0",
            "category_id": row["category_id"],
            "container_extension": "mp4",
            "custom_sid": "",
            "direct_source": "",
        },
    }


# ---------------------------------------------------------------------------
# Upstream fetch helpers (lazy VOD URL resolution + series info)
# ---------------------------------------------------------------------------

def _fetch_vod_url(stream_id: int, tvg_id: str, item_id: int, db: Session) -> Optional[str]:
    item = db.query(Item).filter(Item.id == item_id).first()
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
                "vod_id": tvg_id,
            },
            headers={**_UPSTREAM_HEADERS, "Referer": base},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning(f"Upstream get_vod_info failed for vod {tvg_id}: {exc}")
        return None

    ext = (data.get("movie_data") or {}).get("container_extension") or "mp4"
    url = f"{base}/movie/{item.username}/{item.user_pass}/{tvg_id}.{ext}"
    with _vod_url_cache_lock:
        if len(_vod_url_cache) >= _CACHE_MAX:
            for k in list(_vod_url_cache)[: _CACHE_MAX // 2]:
                del _vod_url_cache[k]
        _vod_url_cache[stream_id] = url
    return url


def _series_info_fallback_from_row(row) -> dict:
    return {
        "info": {
            "name": row["name"],
            "cover": row["logo"],
            "plot": "",
            "cast": "",
            "director": "",
            "genre": "",
            "release_date": "",
            "rating": "0",
            "backdrop_path": [],
            "youtube_trailer": "",
            "episode_run_time": "0",
            "category_id": row["category_id"],
        },
        "episodes": {},
    }


def _fetch_series_info_from_row(row, db: Session) -> dict:
    item = db.query(Item).filter(Item.id == row["item_id"]).first()
    if not item:
        return _series_info_fallback_from_row(row)

    try:
        base = item.server_url.rstrip("/")
        resp = requests.get(
            f"{base}/player_api.php",
            params={
                "username": item.username,
                "password": item.user_pass,
                "action": "get_series_info",
                "series_id": row["tvg_id"],
            },
            headers={**_UPSTREAM_HEADERS, "Referer": base},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning(f"Upstream get_series_info failed for series {row['tvg_id']}: {exc}")
        return _series_info_fallback_from_row(row)

    episodes_out = {}
    for season_num, episodes in data.get("episodes", {}).items():
        season_list = []
        for ep in episodes:
            try:
                upstream_ep_id = int(ep.get("id", 0))
            except (ValueError, TypeError):
                continue
            ext = ep.get("container_extension", "mp4")
            local_id = _episode_id(row["item_id"], upstream_ep_id)
            upstream_url = (
                f"{base}/series/{item.username}/{item.user_pass}/{upstream_ep_id}.{ext}"
            )
            with _episode_cache_lock:
                if len(_episode_cache) >= _CACHE_MAX:
                    for k in list(_episode_cache)[: _CACHE_MAX // 2]:
                        del _episode_cache[k]
                _episode_cache[local_id] = upstream_url
            ep_out = dict(ep)
            ep_out["id"] = str(local_id)
            ep_out["direct_source"] = ""
            season_list.append(ep_out)
        if season_list:
            episodes_out[season_num] = season_list

    info = data.get("info", {})
    info.setdefault("name", row["name"])
    info.setdefault("cover", row["logo"])
    return {"info": info, "episodes": episodes_out}


# ---------------------------------------------------------------------------
# Xtream API endpoint
# ---------------------------------------------------------------------------

async def _handle_player_api(
    username: str,
    password: str,
    action: Optional[str],
    vod_id: Optional[str],
    series_id: Optional[str],
    db: Session,
    item: Item,
) -> Response:
    if not verify_credentials(username, password, item):
        return _unauthorized()

    base_url = _get_base_url()
    cache = await get_xtream_cache(db, item)

    if action is None:
        return JSONResponse(_auth_response(base_url, item))

    if action == "get_live_categories":
        return JSONResponse(cache.live_categories)
    if action == "get_vod_categories":
        return JSONResponse(cache.vod_categories)
    if action == "get_series_categories":
        return JSONResponse(cache.series_categories)

    if action == "get_live_streams":
        return JSONResponse([_stream_to_json(s) for s in cache.live_streams])

    # VOD and series lists are served directly from pre-built JSON files — zero RAM cost
    if action == "get_vod_streams":
        if not os.path.exists(cache.vod_json_path):
            return JSONResponse([])
        return FileResponse(cache.vod_json_path, media_type="application/json")
    if action == "get_series":
        if not os.path.exists(cache.series_json_path):
            return JSONResponse([])
        return FileResponse(cache.series_json_path, media_type="application/json")

    if action == "get_vod_info" and vod_id:
        try:
            sid = int(vod_id)
        except (ValueError, TypeError):
            return JSONResponse({"info": {}, "movie_data": {}})
        row = await asyncio.to_thread(_catalog_lookup, cache.db_path, sid)
        if not row or row["stream_type"] != "movie":
            return JSONResponse({"info": {}, "movie_data": {}})
        return JSONResponse(_vod_info_response_from_row(row))

    if action == "get_series_info" and series_id:
        try:
            sid = int(series_id)
        except (ValueError, TypeError):
            return JSONResponse({"info": {}, "episodes": {}})
        row = await asyncio.to_thread(_catalog_lookup, cache.db_path, sid)
        if not row or row["stream_type"] != "series":
            return JSONResponse({"info": {}, "episodes": {}})
        return JSONResponse(await asyncio.to_thread(_fetch_series_info_from_row, row, db))

    return JSONResponse({"error": "Unknown action"}, status_code=400)


@router.get("/{provider_slug}/player_api.php")
async def player_api_get(
    provider_slug: str,
    username: str = Query(...),
    password: str = Query(...),
    action: Optional[str] = Query(None),
    vod_id: Optional[str] = Query(None),
    series_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    item: Item = Depends(get_item_for_slug),
):
    return await _handle_player_api(username, password, action, vod_id, series_id, db, item)


@router.post("/{provider_slug}/player_api.php")
async def player_api_post(
    provider_slug: str,
    username: str = Form(...),
    password: str = Form(...),
    action: Optional[str] = Form(None),
    vod_id: Optional[str] = Form(None),
    series_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    item: Item = Depends(get_item_for_slug),
):
    return await _handle_player_api(username, password, action, vod_id, series_id, db, item)


# ---------------------------------------------------------------------------
# M3U playlist endpoints
# ---------------------------------------------------------------------------

async def _m3u_generator(cache: XtreamCache, base_url: str, username: str, password: str, slug: str):
    """Async generator — yields M3U lines without holding full catalog in RAM."""
    yield "#EXTM3U\n"
    for s in cache.live_streams:
        url = f"{base_url}/{slug}/live/{username}/{password}/{s.stream_id}.ts"
        yield (
            f'#EXTINF:-1 tvg-id="{s.tvg_id}" tvg-name="{s.name}" '
            f'tvg-logo="{s.logo}" group-title="{s.category_name}",{s.name}\n'
            f"{url}\n"
        )

    # Stream VOD/series from SQLite via thread (avoids blocking the event loop)
    def _fetch_rows(stream_type: str) -> list:
        with _catalog_conn(cache.db_path) as conn:
            conn.row_factory = None  # return plain tuples for speed
            return conn.execute(
                "SELECT stream_id, tvg_id, name, logo, category_name "
                "FROM streams WHERE item_id=? AND stream_type=?",
                (cache.item_id, stream_type),
            ).fetchall()

    for sid, tvg_id, name, logo, cat in await asyncio.to_thread(_fetch_rows, "movie"):
        url = f"{base_url}/{slug}/movie/{username}/{password}/{sid}.mp4"
        yield (
            f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}" '
            f'tvg-logo="{logo}" group-title="{cat}",{name}\n'
            f"{url}\n"
        )

    for sid, tvg_id, name, logo, cat in await asyncio.to_thread(_fetch_rows, "series"):
        url = f"{base_url}/{slug}/series/{username}/{password}/{sid}.m3u8"
        yield (
            f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}" '
            f'tvg-logo="{logo}" group-title="{cat}",{name}\n'
            f"{url}\n"
        )


@router.get("/{provider_slug}/get.php")
async def get_m3u_xtream(
    provider_slug: str,
    username: str = Query(...),
    password: str = Query(...),
    type: str = Query("m3u_plus"),
    output: str = Query("ts"),
    db: Session = Depends(get_db),
    item: Item = Depends(get_item_for_slug),
):
    if not verify_credentials(username, password, item):
        return Response("Unauthorized", status_code=401)
    base_url = _get_base_url()
    cache = await get_xtream_cache(db, item)
    return StreamingResponse(
        _m3u_generator(cache, base_url, username, password, provider_slug),
        media_type="application/x-mpegurl",
        headers={"Content-Disposition": 'attachment; filename="playlist.m3u"'},
    )


@router.get("/{provider_slug}/iptv/playlist.m3u")
async def get_m3u_simple(
    provider_slug: str,
    username: Optional[str] = Query(None),
    password: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    item: Item = Depends(get_item_for_slug),
):
    proxy_user = item.proxy_username or "iptv"
    proxy_pass = item.proxy_password or "iptv"
    if username and password and not verify_credentials(username, password, item):
        return Response("Unauthorized", status_code=401)
    u = username or proxy_user
    p = password or proxy_pass
    base_url = _get_base_url()
    cache = await get_xtream_cache(db, item)
    return StreamingResponse(
        _m3u_generator(cache, base_url, u, p, provider_slug),
        media_type="application/x-mpegurl",
        headers={"Content-Disposition": 'attachment; filename="playlist.m3u"'},
    )


# ---------------------------------------------------------------------------
# EPG redirect
# ---------------------------------------------------------------------------

@router.get("/{provider_slug}/xmltv.php")
async def xmltv_redirect(
    provider_slug: str,
    username: str = Query(...),
    password: str = Query(...),
    item: Item = Depends(get_item_for_slug),
):
    if not verify_credentials(username, password, item):
        return _unauthorized()
    return RedirectResponse(url="/epg.xml", status_code=302)


# ---------------------------------------------------------------------------
# Stream proxy endpoints
# ---------------------------------------------------------------------------

def _stream_live_direct(source_url: str, channel_name: str, client_ip: str,
                        user_agent: str, session_id: str, max_sessions: int,
                        item_id: int | None = None):
    """Generator that proxies a live stream directly from source_url with retry."""
    chunk_size = config.STREAM_CHUNK_KB * 1024
    max_retries = config.STREAM_MAX_RETRIES
    retry_delay = config.STREAM_RETRY_DELAY
    read_timeout = config.STREAM_READ_TIMEOUT
    proxy_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
        "Connection": "keep-alive",
    }

    with _active_streams_lock:
        _active_streams[session_id] = {
            "session_id": session_id,
            "channel": channel_name,
            "channel_name": channel_name,
            "item_id": item_id,
            "client_ip": client_ip,
            "user_agent": user_agent,
            "started_at": time.time(),
            "last_chunk_at": time.time(),
            "bytes_sent": 0,
            "killed": False,
        }

    bytes_sent = 0
    attempt = 0
    session = requests.Session()
    try:
        while attempt <= max_retries:
            if attempt > 0:
                logger.warning(f"Live reconnect {attempt}/{max_retries} for '{channel_name}' after {bytes_sent} bytes")
                time.sleep(retry_delay)
            attempt += 1
            try:
                resp = session.get(source_url, headers=proxy_headers, stream=True,
                                   timeout=(10, read_timeout))
                resp.raise_for_status()
            except Exception as exc:
                logger.warning(f"Live upstream error for '{channel_name}': {exc}")
                continue
            try:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    if chunk:
                        if _active_streams.get(session_id, {}).get("killed"):
                            logger.info(f"Live stream {session_id} ('{channel_name}') killed by admin")
                            return
                        bytes_sent += len(chunk)
                        _active_streams[session_id]["bytes_sent"] = bytes_sent
                        _active_streams[session_id]["last_chunk_at"] = time.time()
                        yield chunk
                break
            except Exception as exc:
                logger.warning(f"Live read error for '{channel_name}': {exc}")
            finally:
                resp.close()
    finally:
        logger.info(f"Live stream ended: '{channel_name}', {bytes_sent} bytes sent")
        _active_streams.pop(session_id, None)


@router.get("/{provider_slug}/live/{username}/{password}/{stream_id_ext:path}")
async def proxy_live(
    provider_slug: str,
    username: str,
    password: str,
    stream_id_ext: str,
    request: Request,
    db: Session = Depends(get_db),
    item: Item = Depends(get_item_for_slug),
):
    if not verify_credentials(username, password, item):
        raise HTTPException(status_code=401, detail="Unauthorized")

    stream_id_str = stream_id_ext.split(".")[0]
    try:
        stream_id = int(stream_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid stream ID")

    cache = await get_xtream_cache(db, item)
    entry = cache.live_stream_map.get(stream_id)
    if not entry:
        logger.warning(f"proxy_live [{item.name}]: stream_id {stream_id} not in cache "
                       f"(cache has {len(cache.live_streams)} live streams)")
        raise HTTPException(status_code=404, detail=f"Live stream {stream_id} not found")

    client_ip = request.client.host if request.client else "unknown"
    if is_ip_blocked(client_ip):
        raise HTTPException(status_code=429, detail="Stream was terminated — reconnect blocked briefly")

    max_sessions = int(item.max_sessions) if item.max_sessions is not None else 1
    active_count = auto_replace_ip_session(client_ip, entry.name, item_id=item.id)
    if active_count >= max_sessions:
        raise HTTPException(status_code=429,
                            detail=f"Session limit reached ({active_count}/{max_sessions} active streams)")

    session_id = str(uuid.uuid4())
    logger.info(f"proxy_live [{item.name}]: '{entry.name}' → direct stream from {entry.url}")
    return StreamingResponse(
        _stream_live_direct(entry.url, entry.name, client_ip,
                            request.headers.get("user-agent", "unknown"),
                            session_id, max_sessions, item_id=item.id),
        media_type="video/mp2t",
        headers={"Cache-Control": "no-cache"},
    )


def _proxy_finite_stream(source_url: str, request: Request, media_type: str, stream_label: str = "VOD", item_id: int = 0):
    """Proxy a finite stream (VOD/series) with Range support for seeking."""
    chunk_size = config.STREAM_CHUNK_KB * 1024
    proxy_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
    }
    range_header = request.headers.get("range")
    proxy_headers["Range"] = range_header or "bytes=0-"

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

    forward_headers = {
        "Cache-Control": "no-cache",
        "Accept-Ranges": "bytes",
    }
    for hdr in ("Content-Range", "Content-Type"):
        val = resp.headers.get(hdr)
        if val:
            forward_headers[hdr] = val

    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")
    session_id = str(uuid.uuid4())

    def generate():
        _active_streams[session_id] = {
            "session_id": session_id,
            "channel": stream_label,
            "item_id": item_id,
            "client_ip": client_ip,
            "user_agent": user_agent,
            "started_at": time.time(),
            "last_chunk_at": time.time(),
            "bytes_sent": 0,
            "killed": False,
        }
        bytes_sent = 0
        try:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if chunk:
                    if _active_streams.get(session_id, {}).get("killed"):
                        logger.info(f"Stream {session_id} ({stream_label}) terminated by admin")
                        return
                    bytes_sent += len(chunk)
                    _active_streams[session_id]["bytes_sent"] = bytes_sent
                    _active_streams[session_id]["last_chunk_at"] = time.time()
                    yield chunk
        except Exception as exc:
            logger.error(f"Stream error mid-transfer for {source_url}: {exc}")
        finally:
            _active_streams.pop(session_id, None)
            resp.close()

    return StreamingResponse(
        generate(),
        status_code=resp.status_code,
        media_type=media_type,
        headers=forward_headers,
    )


@router.get("/{provider_slug}/movie/{username}/{password}/{stream_id_ext:path}")
async def proxy_vod(
    provider_slug: str,
    username: str,
    password: str,
    stream_id_ext: str,
    request: Request,
    db: Session = Depends(get_db),
    item: Item = Depends(get_item_for_slug),
):
    if not verify_credentials(username, password, item):
        raise HTTPException(status_code=401, detail="Unauthorized")

    stream_id_str = stream_id_ext.split(".")[0]
    try:
        stream_id = int(stream_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid stream ID")

    client_ip = request.client.host if request.client else "unknown"
    if is_ip_blocked(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Stream was terminated — reconnect blocked briefly",
            headers={"Retry-After": str(KILL_BLOCK_SECONDS)},
        )

    cache = await get_xtream_cache(db, item)
    row = await asyncio.to_thread(_catalog_lookup, cache.db_path, stream_id)
    if not row or row["stream_type"] != "movie":
        raise HTTPException(status_code=404, detail=f"VOD stream {stream_id} not found")

    provider_item = db.query(Item).filter(Item.id == row["item_id"]).first()
    max_sessions = int(provider_item.max_sessions) if provider_item and provider_item.max_sessions is not None else 1
    active_count = auto_replace_ip_session(client_ip, f"VOD:{stream_id}")
    if active_count >= max_sessions:
        raise HTTPException(
            status_code=429,
            detail=f"Session limit reached ({active_count}/{max_sessions} active streams)",
            headers={"Retry-After": "30"},
        )

    with _vod_url_cache_lock:
        resolved_url = _vod_url_cache.get(stream_id)

    if resolved_url is None:
        resolved_url = await asyncio.to_thread(
            _fetch_vod_url, stream_id, row["tvg_id"], row["item_id"], db
        ) or row["url"]

    result = _proxy_finite_stream(
        resolved_url, request, "video/mp4",
        stream_label=f"VOD:{stream_id}", item_id=row["item_id"],
    )

    if isinstance(result, JSONResponse) and result.status_code == 502:
        with _vod_url_cache_lock:
            _vod_url_cache.pop(stream_id, None)

    return result


@router.get("/{provider_slug}/series/{username}/{password}/{stream_id_ext:path}")
async def proxy_series(
    provider_slug: str,
    username: str,
    password: str,
    stream_id_ext: str,
    request: Request,
    db: Session = Depends(get_db),
    item: Item = Depends(get_item_for_slug),
):
    if not verify_credentials(username, password, item):
        raise HTTPException(status_code=401, detail="Unauthorized")

    stream_id_str = stream_id_ext.split(".")[0]
    try:
        stream_id = int(stream_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid stream ID")

    client_ip = request.client.host if request.client else "unknown"
    if is_ip_blocked(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Stream was terminated — reconnect blocked briefly",
            headers={"Retry-After": str(KILL_BLOCK_SECONDS)},
        )

    max_sessions = int(item.max_sessions) if item.max_sessions is not None else 1
    active_count = auto_replace_ip_session(client_ip, f"Series:{stream_id}")
    if active_count >= max_sessions:
        raise HTTPException(
            status_code=429,
            detail=f"Session limit reached ({active_count}/{max_sessions} active streams)",
            headers={"Retry-After": "30"},
        )

    with _episode_cache_lock:
        upstream_url = _episode_cache.get(stream_id)

    if upstream_url:
        logger.debug(f"Series episode {stream_id} → {upstream_url}")
        return _proxy_finite_stream(upstream_url, request, "video/mp4",
                                    stream_label=f"Series:{stream_id}", item_id=item.id)

    logger.warning(f"Series episode {stream_id} not in episode cache")
    cache = await get_xtream_cache(db, item)
    row = await asyncio.to_thread(_catalog_lookup, cache.db_path, stream_id)
    if not row or row["stream_type"] != "series":
        return JSONResponse({"error": f"Series stream {stream_id} not found"}, status_code=404)

    return _proxy_finite_stream(
        row["url"], request, "application/vnd.apple.mpegurl",
        stream_label=f"Series:{stream_id}", item_id=row["item_id"],
    )


# ---------------------------------------------------------------------------
# Backward-compat: old root-level Xtream paths return a helpful error
# ---------------------------------------------------------------------------

def _provider_not_found_response(db: Session) -> JSONResponse:
    all_items = db.query(Item).all()
    available = [f"/{i.slug}/" for i in all_items if i.slug]
    return JSONResponse(
        {
            "error": "Provider path required. Reconfigure your IPTV app to use a provider-specific URL.",
            "available_providers": available,
            "example": f"{available[0]}player_api.php" if available else "/provider-slug/player_api.php",
        },
        status_code=404,
    )


@router.get("/player_api.php")
async def player_api_legacy(db: Session = Depends(get_db)):
    return _provider_not_found_response(db)


@router.post("/player_api.php")
async def player_api_legacy_post(db: Session = Depends(get_db)):
    return _provider_not_found_response(db)


@router.get("/get.php")
async def get_m3u_legacy(db: Session = Depends(get_db)):
    return _provider_not_found_response(db)


@router.get("/iptv/playlist.m3u")
async def playlist_legacy(db: Session = Depends(get_db)):
    return _provider_not_found_response(db)


@router.get("/xmltv.php")
async def xmltv_legacy(db: Session = Depends(get_db)):
    return _provider_not_found_response(db)
