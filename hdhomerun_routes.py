from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse, Response, HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from models import get_db, Item, get_app_config
from hdhomerun_emulator import HDHomeRunEmulator
import config
import logging
import re
import os
import threading
import time
import uuid
import requests
import subprocess
import json
from typing import Iterator

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Create emulator instance but don't start it yet
hdhomerun_emulator = HDHomeRunEmulator()

# Module-level caches: guide_number -> source URL / display name / item_id (populated by load_channel_lineup)
_channel_source_urls: dict = {}
_channel_names: dict = {}
_channel_item_id: dict = {}   # guide_number -> item_id (for per-provider max_sessions)
_source_url_to_guide: dict = {}  # reverse: source_url -> guide_number
_channel_source_urls_lock = threading.Lock()


def get_guide_for_source_url(url: str) -> str | None:
    with _channel_source_urls_lock:
        return _source_url_to_guide.get(url)

# Active proxy streams — keyed by session UUID, value is session info dict
_active_streams: dict[str, dict] = {}
_active_streams_lock = threading.Lock()

# Sessions with no chunk activity for longer than this are considered dead
_SESSION_STALE_SECONDS = config.STREAM_SESSION_STALE_SECONDS

# IPs blocked after admin kill: ip -> unblock_timestamp
_blocked_ips: dict[str, float] = {}
_blocked_ips_lock = threading.Lock()
KILL_BLOCK_SECONDS = 60


def _purge_stale_sessions():
    """Background thread: remove sessions with no recent chunk activity from the dict."""
    while True:
        time.sleep(_SESSION_STALE_SECONDS)
        cutoff = time.time() - _SESSION_STALE_SECONDS
        with _active_streams_lock:
            stale = [sid for sid, s in _active_streams.items() if s["last_chunk_at"] < cutoff]
            for sid in stale:
                _active_streams.pop(sid, None)
        if stale:
            import logging as _logging
            _logging.getLogger(__name__).debug(f"Purged {len(stale)} stale stream session(s)")


threading.Thread(target=_purge_stale_sessions, daemon=True, name="session-purge").start()


def _live_streams() -> list[dict]:
    """Return sessions that have had recent activity (not stale)."""
    cutoff = time.time() - _SESSION_STALE_SECONDS
    with _active_streams_lock:
        return [s for s in _active_streams.values() if s["last_chunk_at"] >= cutoff]


def get_active_stream_count() -> int:
    return len(_live_streams())


def get_active_streams() -> list[dict]:
    return _live_streams()


def is_ip_blocked(ip: str) -> bool:
    with _blocked_ips_lock:
        until = _blocked_ips.get(ip)
        if until is None:
            return False
        if time.time() < until:
            return True
        del _blocked_ips[ip]
        return False


def auto_replace_ip_session(client_ip: str, new_channel: str, item_id: int | None = None) -> int:
    """Kill any non-killed active sessions from client_ip on the same provider (channel switch).
    No IP block is added. Returns the count of non-killed live streams for this provider after replacement."""
    killed_info = []
    with _active_streams_lock:
        for sid, s in _active_streams.items():
            if (s.get("client_ip") == client_ip and not s.get("killed")
                    and (item_id is None or s.get("item_id") == item_id)):
                s["killed"] = True
                killed_info.append((sid, s.get("channel_name") or s.get("channel", "?")))
    for sid, old_ch in killed_info:
        logger.info(f"Auto-replaced session {sid}: IP {client_ip} switched from '{old_ch}' to '{new_channel}'")
    return sum(
        1 for s in _live_streams()
        if not s.get("killed") and (item_id is None or s.get("item_id") == item_id)
    )


def kill_stream(session_id: str, block_ip: bool = True) -> bool:
    """Mark a session as killed. If block_ip=True, rejects reconnects from that IP for KILL_BLOCK_SECONDS."""
    with _active_streams_lock:
        s = _active_streams.get(session_id)
        if not s:
            return False
        s["killed"] = True
        client_ip = s.get("client_ip")
    if block_ip and client_ip and client_ip not in ("unknown", ""):
        with _blocked_ips_lock:
            _blocked_ips[client_ip] = time.time() + KILL_BLOCK_SECONDS
        logger.info(f"Admin killed stream session={session_id} ip={client_ip} blocked for {KILL_BLOCK_SECONDS}s")
    else:
        logger.info(f"Killed stream session={session_id} (no IP block)")
    return True


def register_extra_channels(url_map: dict, name_map: dict | None = None, item_id_map: dict | None = None):
    """Register source URLs (and optionally display names) for extra channels."""
    with _channel_source_urls_lock:
        _channel_source_urls.update(url_map)
        _source_url_to_guide.update({v: k for k, v in url_map.items()})
        if name_map:
            _channel_names.update(name_map)
        if item_id_map:
            _channel_item_id.update(item_id_map)

def get_advertised_base_url() -> str:
    return config.ADVERTISED_BASE_URL

def load_channel_lineup(db: Session = Depends(get_db)) -> list:
    """Load channels from the global hdhr_filtered_playlist.m3u with de-duplication and explicit numbering preference."""
    channels = []

    # Update device ID based on advertised IP/port
    base_url = get_advertised_base_url()
    import urllib.parse
    parsed = urllib.parse.urlparse(base_url)
    ip = parsed.hostname or "127.0.0.1"
    port = int(parsed.port) if parsed.port else 5005
    hdhomerun_emulator.update_device_id((ip, port))

    from m3u_service import HDHR_PLAYLIST_FILENAME
    hdhr_path = os.path.join(config.M3U_DIR, HDHR_PLAYLIST_FILENAME)
    if not os.path.exists(hdhr_path):
        logger.warning("hdhr_filtered_playlist.m3u not found — HDHR lineup empty")
        return channels

    # Helpers for name normalization
    import unicodedata
    import re as _re
    def _normalize(s: str) -> str:
        s = (s or "").lower().strip()
        s = unicodedata.normalize('NFKD', s)
        s = ''.join(c for c in s if not unicodedata.combining(c))
        s = ' '.join(s.split())
        return s
    def _strict_norm(s: str) -> str:
        return _re.sub(r'[^a-z0-9]+', '', _normalize(s))

    # Track used channel numbers to avoid conflicts and deduplicate by name
    used_guide_numbers = set()
    next_available_number = 1
    channels_by_name = {}
    channel_count = 0

    with open(hdhr_path, 'r') as f:
        lines = f.readlines()

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF'):
            if i + 1 < len(lines):
                # Parse the EXTINF line
                if ',' in line:
                    attrs, name = line.split(',', 1)
                    name = name.strip()

                    # Extract metadata
                    tvg_id = ""
                    tvg_name = ""
                    tvg_chno = ""
                    group = ""
                    item_id = None

                    id_match = re.search(r'tvg-id="([^"]+)"', attrs)
                    if id_match:
                        tvg_id = id_match.group(1)

                    name_match = re.search(r'tvg-name="([^"]+)"', attrs)
                    if name_match:
                        tvg_name = name_match.group(1)

                    chno_match = re.search(r'tvg-chno="([^"]+)"', attrs)
                    if chno_match:
                        tvg_chno = chno_match.group(1)

                    group_match = re.search(r'group-title="([^"]+)"', attrs)
                    if group_match:
                        group = group_match.group(1)

                    logo = ""
                    logo_match = re.search(r'tvg-logo="([^"]+)"', attrs)
                    if logo_match:
                        logo = logo_match.group(1)

                    # x-item-id is embedded by build_hdhr_playlist for stream routing
                    item_id_match = re.search(r'x-item-id="([^"]+)"', attrs)
                    if item_id_match:
                        try:
                            item_id = int(item_id_match.group(1))
                        except ValueError:
                            pass

                    # Prefer tvg-name over channel name if available
                    display_name = tvg_name or name
                    display_name = display_name.replace('_', ' ').strip()

                    url = lines[i + 1].strip()

                    # Determine guide number - prefer explicit tvg-chno when present
                    explicit_number = False
                    if tvg_chno:
                        guide_number = tvg_chno
                        explicit_number = True
                    else:
                        while str(next_available_number) in used_guide_numbers:
                            next_available_number += 1
                        guide_number = str(next_available_number)
                        next_available_number += 1

                    channel_data = {
                        "GuideNumber": guide_number,
                        "GuideName": display_name,
                        "GuideSourceID": tvg_id,
                        "HD": 1,
                        "URL": f"{base_url}/auto/v{guide_number}",
                        "Favorite": 0,
                        "DRM": 0,
                        "VideoCodec": "H264",
                        "AudioCodec": "AAC",
                        "_ExplicitNumber": 1 if explicit_number else 0,
                        "_SourceURL": url,
                        "_ItemId": item_id,
                    }
                    if logo:
                        channel_data["ImageURL"] = logo
                    if group:
                        channel_data["NetworkName"] = group
                        channel_data["NetworkAffiliate"] = group

                    norm_name = _strict_norm(display_name)
                    existing = channels_by_name.get(norm_name)
                    if existing is None:
                        channels_by_name[norm_name] = channel_data
                        used_guide_numbers.add(guide_number)
                        channel_count += 1
                    elif (not existing.get("_ExplicitNumber")) and channel_data.get("_ExplicitNumber"):
                        used_guide_numbers.discard(existing.get("GuideNumber", ""))
                        channels_by_name[norm_name] = channel_data
                        used_guide_numbers.add(guide_number)

            i += 2
        else:
            i += 1

    logger.debug(f"Loaded {channel_count} channels from hdhr_filtered_playlist.m3u")

    # Produce final channel list from dedup map
    channels = list(channels_by_name.values())

    # Update module-level caches before stripping internal fields
    new_urls = {ch["GuideNumber"]: ch["_SourceURL"] for ch in channels if "_SourceURL" in ch}
    new_names = {ch["GuideNumber"]: ch.get("GuideName", ch["GuideNumber"]) for ch in channels}
    new_item_ids = {ch["GuideNumber"]: ch["_ItemId"] for ch in channels if "_ItemId" in ch}
    new_reverse = {v: k for k, v in new_urls.items()}
    global _channel_source_urls, _channel_names, _channel_item_id, _source_url_to_guide
    with _channel_source_urls_lock:
        _channel_source_urls = new_urls
        _channel_names = new_names
        _channel_item_id = new_item_ids
        _source_url_to_guide = new_reverse

    # Remove internal flags
    for ch in channels:
        ch.pop("_ExplicitNumber", None)
        ch.pop("_SourceURL", None)
        ch.pop("_ItemId", None)

    logger.debug(f"Total: Loaded {len(channels)} channels for HDHomeRun lineup")

    if channels:
        logger.debug("First channel example: %s", json.dumps(channels[0], indent=2))

    return channels

@router.get("/auto/v{channel_number:path}")
async def stream_channel(channel_number: str, request: Request, db: Session = Depends(get_db)):
    """Stream proxy for HDHomeRun channel requests from Plex."""
    with _channel_source_urls_lock:
        source_url = _channel_source_urls.get(channel_number)
    if not source_url:
        load_channel_lineup(db)
        with _channel_source_urls_lock:
            source_url = _channel_source_urls.get(channel_number)

    if not source_url:
        raise HTTPException(status_code=404, detail=f"Channel {channel_number} not found")

    client_ip = request.client.host if request.client else "unknown"
    if is_ip_blocked(client_ip):
        logger.warning(f"Stream reconnect blocked for {client_ip} (recently killed)")
        raise HTTPException(status_code=429, detail="Stream was terminated — reconnect blocked briefly", headers={"Retry-After": str(KILL_BLOCK_SECONDS)})

    with _channel_source_urls_lock:
        channel_item_id = _channel_item_id.get(channel_number)

    # Use the explicit streaming provider if configured, otherwise fall back to the channel's own provider
    hdhr_stream_provider_id = get_app_config(db, "hdhr_stream_provider_id")
    if hdhr_stream_provider_id:
        try:
            session_item_id = int(hdhr_stream_provider_id)
        except (ValueError, TypeError):
            session_item_id = channel_item_id
    else:
        session_item_id = channel_item_id

    if session_item_id:
        provider = db.query(Item).filter(Item.id == session_item_id).first()
    else:
        provider = db.query(Item).first()
    max_sessions = int(provider.max_sessions) if provider and provider.max_sessions is not None else 1
    active_count = auto_replace_ip_session(client_ip, f"ch {channel_number}", item_id=session_item_id)
    if active_count >= max_sessions:
        logger.warning(
            f"Stream rejected for channel {channel_number}: {active_count}/{max_sessions} sessions active"
        )
        raise HTTPException(
            status_code=429,
            detail=f"Session limit reached ({active_count}/{max_sessions} active streams)",
            headers={"Retry-After": "30"},
        )

    logger.info(f"Stream start: channel {channel_number} → {source_url}")

    proxy_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
        "Connection": "keep-alive",
    }

    chunk_size      = config.STREAM_CHUNK_KB * 1024
    prebuffer_kb    = config.STREAM_PREBUFFER_KB
    prebuffer_bytes = prebuffer_kb * 1024
    max_retries     = config.STREAM_MAX_RETRIES
    retry_delay     = config.STREAM_RETRY_DELAY
    read_timeout    = config.STREAM_READ_TIMEOUT

    user_agent = request.headers.get("user-agent", "unknown")
    session_id = str(uuid.uuid4())

    with _channel_source_urls_lock:
        channel_name = _channel_names.get(channel_number, channel_number)
    with _active_streams_lock:
        _active_streams[session_id] = {
            "session_id": session_id,
            "channel": channel_number,
            "channel_name": channel_name,
            "item_id": session_item_id,
            "client_ip": client_ip,
            "user_agent": user_agent,
            "started_at": time.time(),
            "last_chunk_at": time.time(),
            "bytes_sent": 0,
            "killed": False,
        }

    def generate() -> Iterator[bytes]:
        bytes_sent = 0
        attempt = 0
        effective_max_retries = max_retries  # local copy so we can extend it once
        url_refreshed = False  # have we already tried refreshing the lineup URL?
        prebuf: list[bytes] = []
        prebuf_size = 0
        prebuffering = prebuffer_bytes > 0
        session = requests.Session()
        # Use a mutable container so inner reconnect logic can update the URL.
        current_url = [source_url]
        bytes_at_last_connect = 0  # track how much was sent when the last connection opened
        try:
            while attempt <= effective_max_retries:
                if attempt > 0:
                    # Re-read the URL on every reconnect — the lineup may have been refreshed
                    # in the background (e.g. by a scheduled M3U fetch) with a new token.
                    with _channel_source_urls_lock:
                        fresh = _channel_source_urls.get(channel_number)
                    if fresh and fresh != current_url[0]:
                        logger.debug(
                            f"Channel {channel_number}: URL changed since stream started, using refreshed URL"
                        )
                        current_url[0] = fresh

                    # If all attempts are spent and we haven't tried a lineup reload yet, do it now.
                    if attempt == effective_max_retries and not url_refreshed:
                        logger.warning(
                            f"Channel {channel_number}: all {effective_max_retries} reconnect attempts failed — "
                            f"forcing lineup reload to refresh URL before final retry"
                        )
                        try:
                            from models import SessionLocal
                            with SessionLocal() as db_refresh:
                                load_channel_lineup(db_refresh)
                            with _channel_source_urls_lock:
                                fresh = _channel_source_urls.get(channel_number)
                            if fresh:
                                current_url[0] = fresh
                            url_refreshed = True
                            effective_max_retries += 1  # grant one extra attempt with the new URL
                        except Exception as reload_exc:
                            logger.error(f"Lineup reload failed: {reload_exc}")

                    logger.warning(
                        f"Reconnect attempt {attempt}/{effective_max_retries} for channel {channel_number} "
                        f"after {bytes_sent} bytes — waiting {retry_delay}s..."
                    )
                    time.sleep(retry_delay)
                    # Don't re-prebuffer if we already started streaming
                    if bytes_sent > 0:
                        prebuffering = False

                try:
                    bytes_at_last_connect = bytes_sent
                    logger.debug(
                        f"Connecting upstream for channel {channel_number} "
                        f"(attempt={attempt} chunk={chunk_size//1024}KB prebuffer={prebuffer_kb}KB read_timeout={read_timeout}s)"
                    )
                    with session.get(
                        current_url[0],
                        headers=proxy_headers,
                        stream=True,
                        timeout=(10, read_timeout),
                    ) as resp:
                        resp.raise_for_status()
                        logger.debug(
                            f"Upstream connected for channel {channel_number}: status={resp.status_code}"
                        )

                        for chunk in resp.iter_content(chunk_size=chunk_size):
                            if not chunk:
                                continue
                            if _active_streams.get(session_id, {}).get("killed"):
                                logger.info(f"Stream {session_id} channel={channel_number} terminated by admin")
                                return
                            if prebuffering:
                                prebuf.append(chunk)
                                prebuf_size += len(chunk)
                                if prebuf_size >= prebuffer_bytes:
                                    logger.debug(
                                        f"Pre-buffer filled ({prebuf_size//1024}KB), "
                                        f"starting stream for channel {channel_number}"
                                    )
                                    for c in prebuf:
                                        bytes_sent += len(c)
                                        yield c
                                    prebuf = []
                                    prebuffering = False
                            else:
                                bytes_sent += len(chunk)
                                s = _active_streams.get(session_id)
                                if s:
                                    s["bytes_sent"] = bytes_sent
                                    s["last_chunk_at"] = time.time()
                                yield chunk

                    # Upstream closed connection cleanly — flush any partial prebuffer then reconnect
                    if prebuf:
                        for c in prebuf:
                            bytes_sent += len(c)
                            yield c
                        prebuf = []
                        prebuffering = False
                    # If this connection delivered substantial data, it was healthy — reset the
                    # retry budget so routine upstream drops don't exhaust it over a long session.
                    delivered = bytes_sent - bytes_at_last_connect
                    if delivered >= 10 * 1024 * 1024:  # 10 MB threshold
                        attempt = 0
                        effective_max_retries = max_retries
                        url_refreshed = False
                    logger.warning(
                        f"Upstream closed stream for channel {channel_number} "
                        f"after {bytes_sent} bytes; reconnecting..."
                    )

                except GeneratorExit:
                    raise
                except Exception as exc:
                    logger.error(
                        f"Stream error for channel {channel_number} after {bytes_sent} bytes "
                        f"(attempt {attempt}/{max_retries}): {exc}"
                    )
                    # Auth/session errors (401, 403, 407, 458) mean the token or session is dead.
                    # 458 is a provider-specific "session rejected" code — treat it like token expiry.
                    http_status = (
                        exc.response.status_code
                        if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None
                        else None
                    )
                    is_session_error = http_status in (401, 403, 407, 458)
                    if is_session_error and not url_refreshed:
                        logger.warning(
                            f"Channel {channel_number}: session error ({http_status}) — "
                            f"forcing immediate lineup reload for fresh URL"
                        )
                        try:
                            from models import SessionLocal
                            with SessionLocal() as db_refresh:
                                load_channel_lineup(db_refresh)
                            with _channel_source_urls_lock:
                                fresh = _channel_source_urls.get(channel_number)
                            if fresh:
                                current_url[0] = fresh
                            url_refreshed = True
                        except Exception as reload_exc:
                            logger.error(f"Lineup reload failed: {reload_exc}")
                    # 458 from a fresh connection (0 bytes) means the provider is rate-limiting or
                    # blocking rapid reconnects — wait longer than the normal retry delay.
                    if http_status == 458 and bytes_sent - bytes_at_last_connect == 0:
                        logger.warning(
                            f"Channel {channel_number}: 458 with 0 bytes — provider likely rate-limiting; "
                            f"sleeping {retry_delay * 4:.0f}s before next attempt"
                        )
                        time.sleep(retry_delay * 4)
                    # Flush any partial prebuffer before retrying
                    if prebuf:
                        for c in prebuf:
                            bytes_sent += len(c)
                            yield c
                        prebuf = []
                        prebuffering = False

                attempt += 1

            logger.error(f"Max retries ({effective_max_retries}) reached for channel {channel_number}, giving up")

        except GeneratorExit:
            logger.debug(f"Client disconnected for channel {channel_number} after {bytes_sent} bytes")
        finally:
            with _active_streams_lock:
                _active_streams.pop(session_id, None)
            session.close()
            logger.info(f"Stream ended: channel {channel_number}, {bytes_sent} bytes sent")

    return StreamingResponse(
        generate(),
        media_type="video/mp2t",
        headers={"Cache-Control": "no-cache, no-store"},
    )


@router.get("/epg.xml")
async def serve_epg(db: Session = Depends(get_db)):
    """
    Serve a combined XMLTV EPG file built from iptv-org US guide data.
    Pass this URL to Plex DVR as your guide source instead of a zip code:
      http://<your-ip>:5005/epg.xml
    Cached for 12 hours; force-refresh via POST /epg/refresh.
    Only includes channels from the configured HDHomeRun provider (or all providers if none set).
    """
    import asyncio
    import multiprocessing as mp
    from epg_manager import run_epg_build, EPG_CACHE_PATH
    hdhr_provider_id = get_app_config(db, "hdhr_provider_id")
    item_ids = [int(hdhr_provider_id)] if hdhr_provider_id else None

    def _build():
        p = mp.Process(target=run_epg_build, args=(False, item_ids), daemon=True)
        p.start()
        p.join()

    await asyncio.to_thread(_build)
    return FileResponse(
        EPG_CACHE_PATH,
        media_type="application/xml",
        headers={"Cache-Control": "max-age=3600", "Access-Control-Allow-Origin": "*"},
    )


@router.post("/epg/refresh")
async def refresh_epg(db: Session = Depends(get_db)):
    """Force a rebuild of the EPG cache from source URLs."""
    import asyncio
    import multiprocessing as mp
    from epg_manager import run_epg_build
    hdhr_provider_id = get_app_config(db, "hdhr_provider_id")
    item_ids = [int(hdhr_provider_id)] if hdhr_provider_id else None

    def _build():
        p = mp.Process(target=run_epg_build, args=(True, item_ids), daemon=True)
        p.start()
        p.join()

    await asyncio.to_thread(_build)
    return RedirectResponse(url="/?success=EPG refreshed successfully", status_code=303)


@router.on_event("startup")
async def startup_event():
    logger.debug("HDHomeRun emulator lazy-start enabled (will start on first HDHR request)")
    # Pre-populate channel lineup so lineup_status.json never needs a DB hit during Plex polling
    try:
        from models import SessionLocal
        with SessionLocal() as db:
            load_channel_lineup(db)
    except Exception as exc:
        logger.warning(f"Could not pre-load channel lineup at startup: {exc}")

def ensure_emulator_started(force=False) -> bool:
    """Start the emulator thread if it's not already running.
    
    Args:
        force: If True, attempt to start even if disabled via environment variable
    """
    try:
        if not hdhomerun_emulator.is_running():
            logger.debug("Starting HDHomeRun emulator thread on demand...")
            result = hdhomerun_emulator.start(force=force)
            if result:
                logger.debug("HDHomeRun emulator thread started")
            else:
                logger.warning("HDHomeRun emulator thread failed to start (may be disabled)")
            return result
        return True
    except Exception as e:
        logger.error(f"Failed to start emulator: {e}")
        return False

@router.post("/hdhr/enable")
async def enable_discovery():
    """Enable HDHomeRun discovery"""
    # Use force=True to override environment variable
    if ensure_emulator_started(force=True):
        success_msg = "HDHomeRun discovery enabled successfully"
        if hdhomerun_emulator.is_env_disabled():
            success_msg += " (Note: Port 1900/UDP may not be exposed - check docker-compose.yml)"
        return RedirectResponse(url=f"/?success={success_msg}", status_code=303)
    return RedirectResponse(url="/?error=Failed to start HDHomeRun emulator - port 1900/UDP may not be exposed", status_code=303)

@router.post("/hdhr/disable")
async def disable_discovery():
    """Disable HDHomeRun discovery"""
    try:
        if hdhomerun_emulator.stop():
            return RedirectResponse(url="/?success=HDHomeRun discovery disabled", status_code=303)
        return RedirectResponse(url="/?error=Failed to stop HDHomeRun emulator", status_code=303)
    except Exception as e:
        logger.error(f"Failed to stop emulator: {e}")
        return RedirectResponse(url="/?error=Failed to stop HDHomeRun emulator", status_code=303)

@router.get("/discover.json")
async def hdhr_discover():
    """Return device discovery info"""
    # Note: SSDP doesn't need to be running for HTTP endpoints to work
    base_url = get_advertised_base_url()
    return {
        "FriendlyName": hdhomerun_emulator.friendly_name,
        "ModelNumber": hdhomerun_emulator.model,
        "FirmwareName": "hdhomerun_iptv",
        "FirmwareVersion": "1.0",
        "DeviceID": hdhomerun_emulator.device_id,
        "DeviceAuth": "iptv_emulator",
        "BaseURL": base_url,
        "LineupURL": f"{base_url}/lineup.json",
        "TunerCount": hdhomerun_emulator.tuner_count
    }

@router.get("/lineup_status.json")
async def hdhr_lineup_status():
    """Return scanning status"""
    return {
        "ScanInProgress": 0,
        "ScanPossible": 1,
        "Source": "Cable",
        "SourceList": ["Cable"],
        "Found": len(_channel_source_urls)  # GIL-safe scalar read
    }

@router.get("/lineup.json")
async def hdhr_lineup(db: Session = Depends(get_db)):
    """Return channel lineup"""
    # Note: SSDP doesn't need to be running for HTTP endpoints to work
    channels = load_channel_lineup(db)
    return channels

@router.get("/channels", response_class=HTMLResponse)
async def channel_browser(request: Request):
    """Channel browser page — click to open streams in VLC or IINA"""
    template = templates.get_template("channels.html")
    rendered = template.render({"request": request})
    return HTMLResponse(content=rendered)

@router.get("/player/{channel_number:path}", response_class=HTMLResponse)
async def player_page(channel_number: str, request: Request, name: str = None, db: Session = Depends(get_db)):
    """Full-page video player for a single channel using mpegts.js"""
    # Ensure channel source URLs are populated
    with _channel_source_urls_lock:
        source_url = _channel_source_urls.get(channel_number)
    if not source_url:
        load_channel_lineup(db)
        with _channel_source_urls_lock:
            source_url = _channel_source_urls.get(channel_number)
    if not source_url:
        raise HTTPException(status_code=404, detail=f"Channel {channel_number} not found")

    channel_name = name or f"Channel {channel_number}"
    import urllib.parse as _up
    # Build the proxy stream URL (goes through /auto/v{num} so the server handles auth)
    base = str(request.base_url).rstrip("/")
    stream_url = f"{base}/auto/v{channel_number}"

    stash_kb    = config.PLAYER_STASH_KB
    latency_max = config.PLAYER_LATENCY_MAX
    latency_min = config.PLAYER_LATENCY_MIN

    template = templates.get_template("player.html")
    rendered = template.render({
        "request": request,
        "channel_number": channel_number,
        "channel_name": channel_name,
        "channel_name_encoded": _up.quote(channel_name),
        "stream_url_encoded": _up.quote(stream_url, safe=""),
        "stream_url_json": json.dumps(stream_url),
        "vlc_href": f"/watch/{channel_number}?name={_up.quote(channel_name)}",
        "stash_kb": stash_kb,
        "latency_max": latency_max,
        "latency_min": latency_min,
    })
    return HTMLResponse(content=rendered)

@router.get("/watch/{channel_number:path}")
async def watch_channel(channel_number: str, request: Request, name: str = None):
    """
    Return a single-entry M3U playlist for the given channel number.
    macOS hands audio/x-mpegurl to whichever player handles .m3u files (VLC, IINA, etc.).
    Pass ?name=Channel+Name to embed a friendly title in the EXTINF line.
    """
    safe_name = name or f"Channel {channel_number}"
    # Build the stream URL from the request's base URL so VLC can reach the proxy
    base = str(request.base_url).rstrip("/")
    stream_url = f"{base}/auto/v{channel_number}"
    # Sanitise name for content-disposition header
    safe_filename = "".join(c for c in safe_name if c.isalnum() or c in " _-").strip() or f"channel_{channel_number}"
    m3u = f"#EXTM3U\n#EXTINF:-1 tvg-name=\"{safe_name}\",{safe_name}\n{stream_url}\n"
    return Response(
        content=m3u,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe_filename}.m3u"'},
    )
