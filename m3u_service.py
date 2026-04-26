"""
m3u_service.py — M3U fetch, filter, and scheduler logic.
Contains no HTTP route handlers; imported by routes.py and main.py.
"""
import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.parse

import requests

import config
from models import Item, SessionLocal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# M3U fetch
# ---------------------------------------------------------------------------

def do_fetch_m3u(item_id: int, db) -> tuple:
    """Fetch and save the full M3U for item_id. Returns (success, message, total_lines)."""
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        return False, f"Item {item_id} not found", 0

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": item.server_url.rstrip('/'),
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    }

    base_url = f"{item.server_url.rstrip('/')}/player_api.php"
    auth_url = f"{base_url}?username={urllib.parse.quote(item.username)}&password={urllib.parse.quote(item.user_pass)}"
    logger.debug(f"Attempting Xtream API auth to {base_url}")

    m3u_content = None
    num_records = 0
    source = "Xtream API"
    try:
        response = requests.get(auth_url, headers=headers, timeout=30)
        response.raise_for_status()
        user_data = response.json()
        logger.debug(f"Xtream API auth response: {json.dumps(user_data, indent=2)[:500]}")

        if user_data.get('user_info', {}).get('auth', 0) != 1:
            logger.warning(f"Invalid Xtream Codes credentials for item {item_id}")
            raise ValueError("Invalid credentials")

        logger.info(f"Authenticated with Xtream Codes for user {item.username}")

        live_streams = requests.get(f"{auth_url}&action=get_live_streams", headers=headers, timeout=30).json()
        vod_streams  = requests.get(f"{auth_url}&action=get_vod_streams",  headers=headers, timeout=30).json()
        series       = requests.get(f"{auth_url}&action=get_series",        headers=headers, timeout=30).json()

        num_records = len(live_streams) + len(vod_streams) + len(series)
        logger.info(f"Fetched {len(live_streams)} live, {len(vod_streams)} VOD, {len(series)} series")

        base = item.server_url.rstrip('/')
        m3u_content = "#EXTM3U\n"
        for stream in live_streams:
            sid  = stream.get('stream_id')
            name = stream.get('name', 'Unknown')
            url  = f"{base}/live/{item.username}/{item.user_pass}/{sid}.ts"
            m3u_content += (
                f'#EXTINF:-1 tvg-id="{sid}" tvg-name="{name}" '
                f'tvg-logo="{stream.get("stream_icon","")}" '
                f'group-title="{stream.get("category_name","Live")}", {name}\n{url}\n'
            )
        for stream in vod_streams:
            sid  = stream.get('stream_id')
            name = stream.get('name', 'Unknown')
            url  = f"{base}/movie/{item.username}/{item.user_pass}/{sid}.mp4"
            m3u_content += (
                f'#EXTINF:-1 tvg-id="{sid}" tvg-name="{name}" '
                f'tvg-logo="{stream.get("stream_icon","")}" '
                f'group-title="{stream.get("category_name","VOD")}", {name}\n{url}\n'
            )
        for serie in series:
            sid  = serie.get('series_id')
            name = serie.get('name', 'Unknown')
            url  = f"{base}/series/{item.username}/{item.user_pass}/{sid}.m3u8"
            m3u_content += (
                f'#EXTINF:-1 tvg-id="{sid}" tvg-name="{name}" '
                f'tvg-logo="{serie.get("cover","")}" '
                f'group-title="Series", {name}\n{url}\n'
            )

    except (requests.exceptions.RequestException, ValueError, json.JSONDecodeError) as e:
        logger.warning(f"Xtream API failed for item {item_id}: {e}, falling back to M3U URL")
        source  = "M3U URL"
        m3u_url = (
            f"{item.server_url.rstrip('/')}/get.php"
            f"?username={urllib.parse.quote(item.username)}"
            f"&password={urllib.parse.quote(item.user_pass)}"
            f"&type=m3u_plus&output=ts"
        )
        for attempt in range(3):
            try:
                response = requests.get(m3u_url, headers=headers, timeout=30)
                response.raise_for_status()
                m3u_content = response.text
                break
            except requests.exceptions.RequestException as e2:
                if attempt == 2:
                    return False, f"Failed to fetch M3U: {e2}", 0
        if not m3u_content or not m3u_content.startswith("#EXTM3U"):
            return False, "Invalid M3U content from provider", 0
        num_records = len(re.findall(r'^#EXTINF', m3u_content, re.MULTILINE))

    os.makedirs(config.M3U_DIR, exist_ok=True)
    m3u_path = os.path.join(config.M3U_DIR, f"xtream_playlist_{item_id}.m3u")
    tmp_m3u  = m3u_path + ".tmp"
    with open(tmp_m3u, "w", encoding="utf-8") as f:
        f.write(m3u_content)
    os.replace(tmp_m3u, m3u_path)

    total_lines = len(m3u_content.splitlines())
    logger.info(f"Saved {source} playlist for item {item_id} ({num_records} records, {total_lines} lines)")

    # Fetch provider EPG
    epg_url = (
        f"{item.server_url.rstrip('/')}/xmltv.php"
        f"?username={urllib.parse.quote(item.username)}"
        f"&password={urllib.parse.quote(item.user_pass)}"
    )
    try:
        epg_resp = requests.get(epg_url, headers=headers, timeout=30)
        epg_resp.raise_for_status()
        epg_path = os.path.join(config.M3U_DIR, f"epg_{item_id}.xml")
        tmp_epg  = epg_path + ".tmp"
        with open(tmp_epg, "w", encoding="utf-8") as f:
            f.write(epg_resp.text)
        os.replace(tmp_epg, epg_path)
        logger.debug(f"Saved EPG for item {item_id}")
    except requests.exceptions.RequestException as e:
        logger.warning(f"EPG fetch failed for item {item_id}: {e}")

    return True, f"Saved {num_records} records ({total_lines} lines) from {source}", total_lines


# ---------------------------------------------------------------------------
# M3U auto-refresh scheduler
# ---------------------------------------------------------------------------
_scheduler_started = False


def start_m3u_scheduler():
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def _run():
        while True:
            time.sleep(1800)  # check every 30 minutes
            try:
                with SessionLocal() as db:
                    items = db.query(Item).all()
                    for item in items:
                        interval_h = item.m3u_refresh_hours or 0
                        if interval_h <= 0:
                            continue
                        playlist_path = os.path.join(config.M3U_DIR, f"xtream_playlist_{item.id}.m3u")
                        try:
                            age_h = (time.time() - os.path.getmtime(playlist_path)) / 3600
                        except FileNotFoundError:
                            continue  # never fetched — skip until user does it manually
                        if age_h >= interval_h:
                            logger.debug(
                                f"Scheduler: refreshing M3U for item {item.id} "
                                f"(age={age_h:.1f}h >= interval={interval_h}h)"
                            )
                            ok, msg, _ = do_fetch_m3u(item.id, db)
                            logger.info(f"Scheduler: item {item.id} fetch {'ok' if ok else 'failed'}: {msg}")
                            if ok:
                                from epg_manager import get_epg
                                get_epg(True)
            except Exception as e:
                logger.error(f"M3U scheduler error: {e}")

    t = threading.Thread(target=_run, name="m3u-scheduler", daemon=True)
    t.start()
    logger.info("M3U auto-refresh scheduler started (checks every 30 min)")


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------

def normalize_channel(s: str) -> str:
    s = s.lower().strip()
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return ' '.join(s.split())


def strict_normalize(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', normalize_channel(s))


def build_filter_config(item) -> tuple:
    """Parse item filter fields into typed structures used by apply_m3u_filter."""
    languages = [l.strip().lower() for l in (item.languages or "").split(",") if l.strip()]

    includes_map: dict[str, str | None] = {}
    raw_includes: list[tuple] = []
    for inc in (item.includes or "").split(","):
        inc = inc.strip()
        if not inc:
            continue
        if '|' in inc:
            num, name = inc.split('|', 1)
            raw_includes.append((num.strip(), name.strip()))
            includes_map[strict_normalize(name)] = num.strip()
        else:
            raw_includes.append((None, inc))
            includes_map[strict_normalize(inc)] = None

    excludes = [ex.strip().lower() for ex in (item.excludes or "").split(",") if ex.strip()]
    has_wildcard_exclude = "*" in excludes
    return languages, includes_map, raw_includes, excludes, has_wildcard_exclude


def apply_m3u_filter(
    lines: list[str],
    languages: list[str],
    includes_map: dict,
    excludes: list[str],
    has_wildcard_exclude: bool,
) -> tuple[str, int, int]:
    """Walk M3U lines and apply language/include/exclude rules.

    Returns (filtered_content, kept_count, total_count).
    """
    input_record_count = sum(1 for ln in lines if ln.startswith("#EXTINF"))
    parts = ["#EXTM3U\n"]
    num_records = 0
    i = 1 if (lines and lines[0].strip() == "#EXTM3U") else 0

    while i < len(lines):
        if not (lines[i].startswith("#EXTINF") and i + 1 < len(lines) and not lines[i + 1].startswith("#")):
            i += 1
            continue

        extinf, url = lines[i], lines[i + 1]

        attributes: dict[str, str] = {}
        channel_name = ""
        if " " in extinf and "," in extinf:
            attr_part, channel_name = extinf.split(",", 1)
            for key, value in re.findall(r'(\S+?)="([^"]*)"', attr_part):
                attributes[key.lower()] = value.lower()
        else:
            channel_name = extinf.split(",", 1)[1] if "," in extinf else ""
        tvg_name = attributes.get('tvg-name', '')

        # 1. Language filter
        if languages:
            channel_language = ""
            if " - " in tvg_name:
                channel_language = tvg_name.split(" - ")[0].strip().lower()
            else:
                tvg_lower = tvg_name.lower()
                for lang in languages:
                    if tvg_lower.startswith(lang + ":") or tvg_lower.startswith(lang + " "):
                        channel_language = lang
                        break
            if channel_language and channel_language not in languages:
                i += 2
                continue

        # 2. Exclude logic
        search_text = normalize_channel(f"{tvg_name} {channel_name}")
        excluded = has_wildcard_exclude or any(
            ex and normalize_channel(ex) in search_text for ex in excludes
        )

        # 3. Include logic
        included = False
        chno_to_apply = None
        if includes_map:
            for cand in (strict_normalize(channel_name), strict_normalize(tvg_name)):
                if cand in includes_map:
                    included, chno_to_apply = True, includes_map[cand]
                    break
                for inc_key, num in includes_map.items():
                    if cand in (inc_key + sfx for sfx in ("hd", "4k", "fhd", "uhd")):
                        included, chno_to_apply = True, num
                        break
                if included:
                    break

        # 4. Emit or skip
        if includes_map:
            if included:
                if chno_to_apply:
                    extinf = re.sub(r'\s*tvg-chno="[^"]*"', '', extinf)
                    idx = extinf.find(',')
                    insert = f' tvg-chno="{chno_to_apply}"'
                    extinf = (extinf[:idx] + insert + extinf[idx:]) if idx != -1 else (extinf + insert)
                parts.append(f"{extinf}\n{url}\n")
                num_records += 1
        elif not excluded:
            parts.append(f"{extinf}\n{url}\n")
            num_records += 1

        i += 2

    return "".join(parts), num_records, input_record_count
