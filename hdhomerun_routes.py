from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse
from sqlalchemy.orm import Session
from models import get_db, Item
from hdhomerun_emulator import HDHomeRunEmulator
import logging
import re
import os
import requests
import subprocess
import json
from typing import Iterator

logger = logging.getLogger(__name__)
router = APIRouter()

# Create emulator instance but don't start it yet
hdhomerun_emulator = HDHomeRunEmulator()

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
    scheme = os.getenv("HDHR_SCHEME", "http")
    port = os.getenv("HDHR_ADVERTISE_PORT", "5005")
    return f"{scheme}://{host}:{port}"

def load_channel_lineup(db: Session = Depends(get_db)) -> list:
    """Load and merge channels from all filtered M3U files"""
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

    # Track used channel numbers to avoid conflicts
    used_guide_numbers = set()
    next_available_number = 1

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

                        # Prefer tvg-name over channel name if available
                        display_name = tvg_name or name
                        # Clean up common name issues
                        display_name = display_name.replace('_', ' ').strip()

                        # Get the URL
                        url = lines[i + 1].strip()

                        # Determine guide number - avoid conflicts across configs
                        if tvg_chno and tvg_chno not in used_guide_numbers:
                            guide_number = tvg_chno
                        else:
                            # Find next available sequential number
                            while str(next_available_number) in used_guide_numbers:
                                next_available_number += 1
                            guide_number = str(next_available_number)
                            next_available_number += 1

                        used_guide_numbers.add(guide_number)

                        # Add required fields for Plex
                        channel_data = {
                            "GuideNumber": guide_number,
                            "GuideName": display_name,
                            "GuideSourceID": tvg_id,  # Important for EPG matching
                            "HD": 1,
                            "URL": url,
                            "Favorite": 0,
                            "DRM": 0,
                            "VideoCodec": "H264",
                            "AudioCodec": "AAC"
                        }

                        # Optional group/network info
                        if group:
                            channel_data["NetworkName"] = group
                            channel_data["NetworkAffiliate"] = group

                        channels.append(channel_data)
                        config_channel_count += 1

                i += 2
            else:
                i += 1

        logger.info(f"  Loaded {config_channel_count} channels from '{item.name}'")

    logger.info(f"Total: Loaded {len(channels)} channels for HDHomeRun lineup from {len(items)} configuration(s)")

    # Log the first few channels for debugging
    if channels:
        logger.info("First channel example:")
        logger.info(json.dumps(channels[0], indent=2))

    return channels

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
