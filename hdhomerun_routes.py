from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from models import get_db, Item
import logging
import re
import os

logger = logging.getLogger(__name__)

router = APIRouter()

from hdhomerun_emulator import hdhomerun_emulator

# Don't auto-start here - let the startup event handle it

@router.on_event("startup")
async def startup_event():
    """Start HDHomeRun emulator when FastAPI starts"""
    logger.info("Starting HDHomeRun emulator on startup...")
    hdhomerun_emulator.start()

@router.get("/discover.json")
async def discover_json():
    host_ip = hdhomerun_emulator.get_host_ip()
    base_url = f"http://{host_ip}:5005"
    
    return {
        "FriendlyName": hdhomerun_emulator.friendly_name,
        "ModelNumber": hdhomerun_emulator.model,
        "FirmwareName": "hdhomerun3_atsc",
        "FirmwareVersion": "20220830",
        "DeviceID": hdhomerun_emulator.device_id,
        "DeviceAuth": "iptv_emulator",
        "BaseURL": base_url,
        "LineupURL": f"{base_url}/lineup.json",
        "TunerCount": hdhomerun_emulator.tuner_count
    }

@router.get("/lineup.json")
async def lineup_json(db: Session = Depends(get_db)):
    """HDHomeRun channel lineup - ONLY FILTERED CHANNELS"""
    items = db.query(Item).all()
    lineup = []
    
    channel_num = 1
    
    for item in items:
        filtered_m3u_path = f"/app/m3u_files/filtered_playlist_{item.id}.m3u"
        
        if not os.path.exists(filtered_m3u_path):
            continue
            
        try:
            with open(filtered_m3u_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            lines = content.split('\n')
            for i in range(len(lines)):
                if lines[i].startswith('#EXTINF'):
                    if i + 1 < len(lines) and not lines[i + 1].startswith('#'):
                        stream_url = lines[i + 1].strip()
                        channel_name = "Unknown"
                        
                        if ',' in lines[i]:
                            channel_name = lines[i].split(',', 1)[1].strip()
                        
                        tvg_name = channel_name
                        attr_match = re.search(r'tvg-name="([^"]*)"', lines[i])
                        if attr_match:
                            tvg_name = attr_match.group(1)
                        
                        lineup.append({
                            "GuideNumber": str(channel_num),
                            "GuideName": tvg_name[:50],
                            "URL": f"/auto/v{channel_num}",
                        })
                        channel_num += 1
                        
        except Exception as e:
            logger.error(f"Error processing filtered M3U for item {item.id}: {e}")
            continue
    
    logger.info(f"HDHomeRun lineup: {len(lineup)} filtered channels")
    return JSONResponse(content=lineup)

@router.get("/auto/v{channel}")
async def stream_channel(channel: int, db: Session = Depends(get_db)):
    items = db.query(Item).all()
    
    current_channel = 1
    
    for item in items:
        filtered_m3u_path = f"/app/m3u_files/filtered_playlist_{item.id}.m3u"
        
        if not os.path.exists(filtered_m3u_path):
            continue
            
        try:
            with open(filtered_m3u_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            lines = content.split('\n')
            for i in range(len(lines)):
                if lines[i].startswith('#EXTINF'):
                    if i + 1 < len(lines) and not lines[i + 1].startswith('#'):
                        if current_channel == channel:
                            stream_url = lines[i + 1].strip()
                            return RedirectResponse(url=stream_url)
                        current_channel += 1
                        
        except Exception as e:
            logger.error(f"Error streaming channel {channel}: {e}")
            continue
    
    raise HTTPException(status_code=404, detail=f"Channel {channel} not found")

@router.get("/lineup_status.json")
async def lineup_status():
    return {
        "ScanInProgress": 0,
        "ScanPossible": 0,
        "Source": "Cable",
        "SourceList": ["Cable"]
    }

@router.get("/debug")
async def hdhomerun_debug():
    host_ip = hdhomerun_emulator.get_host_ip()
    return {
        "status": "running",
        "host_ip": host_ip,
        "manual_plex_url": f"http://{host_ip}:5005"
    }
