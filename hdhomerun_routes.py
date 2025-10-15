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
    """Load channels from filtered M3U file for first item"""
    channels = []
    try:
        item = db.query(Item).first()
        if not item:
            return channels
        
        filtered_path = os.path.join("/app/m3u_files", f"filtered_playlist_{item.id}.m3u")
        if not os.path.exists(filtered_path):
            return channels
            
        with open(filtered_path, 'r') as f:
            lines = f.readlines()
            
        i = 0
        channel_number = 1
        
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
                        
                        # Use tvg-chno if available, otherwise use sequential channel_number
                        guide_number = tvg_chno if tvg_chno else str(channel_number)
                        
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
                        channel_number += 1
                        
                        logger.debug(f"Added channel: {display_name} ({channel_number-1}) - ID: {tvg_id}")
                i += 2
            else:
                i += 1
                
        logger.info(f"Loaded {len(channels)} channels for HDHomeRun lineup")
        
        # Log the first few channels for debugging
        if channels:
            logger.info("First channel example:")
            logger.info(json.dumps(channels[0], indent=2))
            
    except Exception as e:
        logger.error(f"Error loading channel lineup: {e}")
        logger.exception(e)  # Log full traceback
    
    return channels

@router.on_event("startup")
async def startup_event():
    logger.info("HDHomeRun emulator lazy-start enabled (will start on first HDHR request)")

def ensure_emulator_started() -> bool:
    """Start the emulator thread if it's not already running."""
    try:
        if not hdhomerun_emulator.is_running():
            logger.info("Starting HDHomeRun emulator thread on demand...")
            hdhomerun_emulator.start()
            logger.info("HDHomeRun emulator thread started")
        return True
    except Exception as e:
        logger.error(f"Failed to start emulator: {e}")
        return False

@router.post("/hdhr/enable")
async def enable_discovery():
    """Enable HDHomeRun discovery"""
    if ensure_emulator_started():
        return RedirectResponse(url="/", status_code=303)
    return RedirectResponse(url="/?error=Failed+to+start+HDHomeRun+emulator", status_code=303)

@router.post("/hdhr/disable")
async def disable_discovery():
    """Disable HDHomeRun discovery"""
    try:
        hdhomerun_emulator.stop()
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        logger.error(f"Failed to stop emulator: {e}")
        return RedirectResponse(url="/?error=Failed+to+stop+HDHomeRun+emulator", status_code=303)

@router.get("/discover.json")
async def hdhr_discover(db: Session = Depends(get_db)):
    """Return device discovery info"""
    if not ensure_emulator_started():
        raise HTTPException(status_code=503, detail="HDHomeRun emulator not running")
        
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
    if not ensure_emulator_started():
        raise HTTPException(status_code=503, detail="HDHomeRun emulator not running")
        
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
    if not ensure_emulator_started():
        raise HTTPException(status_code=503, detail="HDHomeRun emulator not running")
        
    return load_channel_lineup(db)

@router.post("/lineup.post")
async def hdhr_lineup_post(request: Request, db: Session = Depends(get_db)):
    """Handle lineup commands"""
    if not ensure_emulator_started():
        raise HTTPException(status_code=503, detail="HDHomeRun emulator not running")
    
    form = await request.form()
    scan = form.get("scan")
    
    if scan == "start":
        # Reload channels
        channels = load_channel_lineup(db)
        logger.info(f"Scan started - loaded {len(channels)} channels")
        logger.info("Channel details:")
        for ch in channels[:3]:  # Log first 3 channels
            logger.info(json.dumps(ch, indent=2))
        return {"Status": "Success", "Progress": 100}
    elif scan == "abort":
        return {"Status": "Success"}
    else:
        raise HTTPException(status_code=400, detail="Invalid scan command")

@router.get("/hdhr/debug_lineup")
async def debug_lineup(db: Session = Depends(get_db)):
    """Debug endpoint to view channel lineup"""
    channels = load_channel_lineup(db)
    return {
        "channel_count": len(channels),
        "first_three_channels": channels[:3] if channels else [],
        "lineup_status": {
            "ScanInProgress": 0,
            "ScanPossible": 1,
            "Source": "Cable",
            "SourceList": ["Cable"],
            "Found": len(channels)
        }
    }
