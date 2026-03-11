from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse, Response, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from models import get_db, Item
from hdhomerun_emulator import HDHomeRunEmulator
import logging
import re
import os
import time
import requests
import subprocess
import json
from typing import Iterator

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Create emulator instance but don't start it yet
hdhomerun_emulator = HDHomeRunEmulator()

# Module-level cache: guide_number -> real source URL (populated by load_channel_lineup)
_channel_source_urls: dict = {}

def get_advertised_base_url() -> str:
    """
    Returns the public BaseURL we want Plex to use when calling us.
    Prefer environment variables; fall back to emulator-detected host IP.
    """
    host = (
        os.getenv("HDHR_ADVERTISE_HOST")
        or os.getenv("PUBLIC_HOST")
        or "127.0.0.1"  # Safe default, will be updated when emulator starts
    )
    scheme = os.getenv("HDHR_SCHEME") or "http"
    # Prefer explicit advertised port; otherwise fall back to APP_PORT if provided
    port = os.getenv("HDHR_ADVERTISE_PORT") or os.getenv("APP_PORT") or "5005"
    return f"{scheme}://{host}:{port}"

def load_channel_lineup(db: Session = Depends(get_db)) -> list:
    """Load and merge channels from all filtered M3U files with de-duplication and explicit numbering preference"""
    channels = []
    # Get ALL items, not just the first one
    items = db.query(Item).all()
    if not items:
        logger.warning("No IPTV configurations found")
        return channels

    # Update device ID based on advertised IP/port
    base_url = get_advertised_base_url()
    import urllib.parse
    parsed = urllib.parse.urlparse(base_url)
    ip = parsed.hostname or "127.0.0.1"
    port = int(parsed.port) if parsed.port else 5005
    hdhomerun_emulator.update_device_id((ip, port))

    logger.info(f"Loading channels from {len(items)} IPTV configuration(s)")

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

    for item in items:
        filtered_path = os.path.join("/app/m3u_files", f"filtered_playlist_{item.id}.m3u")
        if not os.path.exists(filtered_path):
            logger.warning(f"Filtered M3U not found for config '{item.name}' (ID {item.id})")
            continue

        logger.info(f"Loading channels from '{item.name}' ({filtered_path})")

        with open(filtered_path, 'r') as f:
            lines = f.readlines()

        i = 0
        config_channel_count = 0

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

                        # Prefer tvg-name over channel name if available
                        display_name = tvg_name or name
                        # Clean up common name issues
                        display_name = display_name.replace('_', ' ').strip()

                        # Get the URL
                        url = lines[i + 1].strip()

                        # Determine guide number - prefer explicit tvg-chno when present
                        explicit_number = False
                        if tvg_chno:
                            guide_number = tvg_chno
                            explicit_number = True
                        else:
                            # Find next available sequential number avoiding conflicts
                            while str(next_available_number) in used_guide_numbers:
                                next_available_number += 1
                            guide_number = str(next_available_number)
                            next_available_number += 1

                        # We'll manage used_guide_numbers when finalizing insert/replace

                        # Add required fields for Plex
                        channel_data = {
                            "GuideNumber": guide_number,
                            "GuideName": display_name,
                            "GuideSourceID": tvg_id,  # Important for EPG matching
                            "HD": 1,
                            # Use local proxy URL so Plex hits this server, not the provider directly
                            "URL": f"{base_url}/auto/v{guide_number}",
                            "Favorite": 0,
                            "DRM": 0,
                            "VideoCodec": "H264",
                            "AudioCodec": "AAC",
                            # Internal fields stripped before returning lineup
                            "_ExplicitNumber": 1 if explicit_number else 0,
                            "_SourceURL": url,
                        }
                        if logo:
                            channel_data["ImageURL"] = logo

                        # Optional group/network info
                        if group:
                            channel_data["NetworkName"] = group
                            channel_data["NetworkAffiliate"] = group

                        # Deduplicate by normalized name; prefer explicit-number entries
                        norm_name = _strict_norm(display_name)
                        existing = channels_by_name.get(norm_name)
                        if existing is None:
                            channels_by_name[norm_name] = channel_data
                            used_guide_numbers.add(guide_number)
                            config_channel_count += 1
                        else:
                            if (not existing.get("_ExplicitNumber")) and channel_data.get("_ExplicitNumber"):
                                # Replace non-explicit with explicit; update used numbers
                                try:
                                    used_guide_numbers.discard(existing.get("GuideNumber", ""))
                                except Exception:
                                    pass
                                channels_by_name[norm_name] = channel_data
                                used_guide_numbers.add(guide_number)
                            else:
                                # Keep existing (either both non-explicit or existing already explicit)
                                pass

                i += 2
            else:
                i += 1

        logger.info(f"  Loaded {config_channel_count} channels from '{item.name}'")

    # Produce final channel list from dedup map
    channels = list(channels_by_name.values())

    # Update module-level source URL cache before stripping internal fields
    global _channel_source_urls
    _channel_source_urls = {
        ch["GuideNumber"]: ch["_SourceURL"]
        for ch in channels
        if "_SourceURL" in ch
    }

    # Remove internal flags
    for ch in channels:
        ch.pop("_ExplicitNumber", None)
        ch.pop("_SourceURL", None)

    logger.info(f"Total: Loaded {len(channels)} channels for HDHomeRun lineup from {len(items)} configuration(s)")

    # Log the first few channels for debugging
    if channels:
        logger.info("First channel example:")
        logger.info(json.dumps(channels[0], indent=2))

    return channels

@router.get("/auto/v{channel_number:path}")
async def stream_channel(channel_number: str, request: Request, db: Session = Depends(get_db)):
    """Stream proxy for HDHomeRun channel requests from Plex."""
    source_url = _channel_source_urls.get(channel_number)
    if not source_url:
        load_channel_lineup(db)
        source_url = _channel_source_urls.get(channel_number)

    if not source_url:
        raise HTTPException(status_code=404, detail=f"Channel {channel_number} not found")

    logger.info(f"Stream request for channel {channel_number} method={request.method}")
    logger.info(f"Proxying channel {channel_number} -> {source_url}")

    proxy_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
        "Connection": "keep-alive",
    }

    # Tuning knobs — all overridable via environment variables
    chunk_size      = int(os.getenv("STREAM_CHUNK_KB",      "64"))  * 1024
    prebuffer_kb    = int(os.getenv("STREAM_PREBUFFER_KB",  "512"))
    prebuffer_bytes = prebuffer_kb * 1024
    max_retries     = int(os.getenv("STREAM_MAX_RETRIES",   "5"))
    retry_delay     = float(os.getenv("STREAM_RETRY_DELAY", "3"))    # seconds between reconnects
    read_timeout    = float(os.getenv("STREAM_READ_TIMEOUT","30"))   # seconds without data before reconnect

    def generate() -> Iterator[bytes]:
        bytes_sent = 0
        attempt = 0
        prebuf: list[bytes] = []
        prebuf_size = 0
        prebuffering = prebuffer_bytes > 0
        session = requests.Session()
        try:
            while attempt <= max_retries:
                if attempt > 0:
                    logger.warning(
                        f"Reconnect attempt {attempt}/{max_retries} for channel {channel_number} "
                        f"after {bytes_sent} bytes — waiting {retry_delay}s..."
                    )
                    time.sleep(retry_delay)
                    # Don't re-prebuffer if we already started streaming
                    if bytes_sent > 0:
                        prebuffering = False

                try:
                    logger.info(
                        f"Connecting upstream for channel {channel_number} "
                        f"(attempt={attempt} chunk={chunk_size//1024}KB prebuffer={prebuffer_kb}KB read_timeout={read_timeout}s)"
                    )
                    with session.get(
                        source_url,
                        headers=proxy_headers,
                        stream=True,
                        timeout=(10, read_timeout),
                    ) as resp:
                        resp.raise_for_status()
                        logger.info(
                            f"Upstream connected for channel {channel_number}: status={resp.status_code}"
                        )

                        for chunk in resp.iter_content(chunk_size=chunk_size):
                            if not chunk:
                                continue
                            if prebuffering:
                                prebuf.append(chunk)
                                prebuf_size += len(chunk)
                                if prebuf_size >= prebuffer_bytes:
                                    logger.info(
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
                                yield chunk

                    # Upstream closed connection cleanly — flush any partial prebuffer then reconnect
                    if prebuf:
                        for c in prebuf:
                            bytes_sent += len(c)
                            yield c
                        prebuf = []
                        prebuffering = False
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
                    # Flush any partial prebuffer before retrying
                    if prebuf:
                        for c in prebuf:
                            bytes_sent += len(c)
                            yield c
                        prebuf = []
                        prebuffering = False

                attempt += 1

            logger.error(f"Max retries ({max_retries}) reached for channel {channel_number}, giving up")

        except GeneratorExit:
            logger.info(f"Client disconnected for channel {channel_number} after {bytes_sent} bytes")
        finally:
            session.close()
            logger.info(f"Stream ended for channel {channel_number}, total bytes sent: {bytes_sent}")

    return StreamingResponse(
        generate(),
        media_type="video/mp2t",
        headers={"Cache-Control": "no-cache, no-store"},
    )


@router.get("/epg.xml")
async def serve_epg():
    """
    Serve a combined XMLTV EPG file built from iptv-org US guide data.
    Pass this URL to Plex DVR as your guide source instead of a zip code:
      http://<your-ip>:5005/epg.xml
    Cached for 12 hours; force-refresh via POST /epg/refresh.
    """
    import asyncio
    from epg_manager import get_epg
    from fastapi.responses import Response as FastResponse
    xml_content = await asyncio.get_event_loop().run_in_executor(None, get_epg)
    return FastResponse(
        content=xml_content,
        media_type="application/xml",
        headers={"Cache-Control": "max-age=3600", "Access-Control-Allow-Origin": "*"},
    )


@router.post("/epg/refresh")
async def refresh_epg():
    """Force a rebuild of the EPG cache from source URLs."""
    import asyncio
    from epg_manager import get_epg
    await asyncio.get_event_loop().run_in_executor(None, lambda: get_epg(force_refresh=True))
    return RedirectResponse(url="/?success=EPG refreshed successfully", status_code=303)


@router.on_event("startup")
async def startup_event():
    logger.info("HDHomeRun emulator lazy-start enabled (will start on first HDHR request)")

def ensure_emulator_started(force=False) -> bool:
    """Start the emulator thread if it's not already running.
    
    Args:
        force: If True, attempt to start even if disabled via environment variable
    """
    try:
        if not hdhomerun_emulator.is_running():
            logger.info("Starting HDHomeRun emulator thread on demand...")
            result = hdhomerun_emulator.start(force=force)
            if result:
                logger.info("HDHomeRun emulator thread started")
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
async def hdhr_lineup_status(db: Session = Depends(get_db)):
    """Return scanning status"""
    # Note: SSDP doesn't need to be running for HTTP endpoints to work
    channels = load_channel_lineup(db)
    return {
        "ScanInProgress": 0,
        "ScanPossible": 1,
        "Source": "Cable",
        "SourceList": ["Cable"],
        "Found": len(channels)
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
    return templates.TemplateResponse("channels.html", {"request": request})

@router.get("/player/{channel_number:path}", response_class=HTMLResponse)
async def player_page(channel_number: str, request: Request, name: str = None, db: Session = Depends(get_db)):
    """Full-page video player for a single channel using mpegts.js"""
    # Ensure channel source URLs are populated
    if not _channel_source_urls:
        load_channel_lineup(db)
    source_url = _channel_source_urls.get(channel_number)
    if not source_url:
        raise HTTPException(status_code=404, detail=f"Channel {channel_number} not found")

    channel_name = name or f"Channel {channel_number}"
    import urllib.parse as _up
    # Build the proxy stream URL (goes through /auto/v{num} so the server handles auth)
    base = str(request.base_url).rstrip("/")
    stream_url = f"{base}/auto/v{channel_number}"

    # Pass client-side buffer config from env to the template
    stash_kb   = int(os.getenv("PLAYER_STASH_KB",   "1024"))  # mpegts.js stash buffer size
    latency_max = float(os.getenv("PLAYER_LATENCY_MAX", "30.0"))  # max live buffer latency seconds
    latency_min = float(os.getenv("PLAYER_LATENCY_MIN",  "5.0"))  # min live buffer remain seconds

    return templates.TemplateResponse("player.html", {
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
