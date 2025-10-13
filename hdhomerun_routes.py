from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session
from models import get_db, Item
import logging
import re
import os
import requests
import subprocess
from typing import Iterator

logger = logging.getLogger(__name__)
router = APIRouter()

from hdhomerun_emulator import hdhomerun_emulator


# ---- helpers ---------------------------------------------------------------

def get_advertised_base_url() -> str:
    """
    Returns the public BaseURL we want Plex to use when calling us.
    Prefer environment variables; fall back to emulator-detected host IP.
    """
    host = (
        os.getenv("HDHR_ADVERTISE_HOST")
        or os.getenv("PUBLIC_HOST")
        or hdhomerun_emulator.get_host_ip()
    )
    scheme = os.getenv("HDHR_SCHEME", "http")
    port = os.getenv("HDHR_ADVERTISE_PORT", "5005")
    return f"{scheme}://{host}:{port}"


# ---- lifecycle -------------------------------------------------------------

@router.on_event("startup")
async def startup_event():
    logger.info("Starting HDHomeRun emulator on startup...")
    hdhomerun_emulator.start()


# ---- discovery -------------------------------------------------------------

@router.get("/discover.json")
async def discover_json():
    base_url = get_advertised_base_url()
    return {
        "FriendlyName": hdhomerun_emulator.friendly_name,
        "ModelNumber": hdhomerun_emulator.model,
        "FirmwareName": "hdhomerun3_atsc",
        "FirmwareVersion": "20220830",
        "DeviceID": hdhomerun_emulator.device_id,
        "DeviceAuth": "iptv_emulator",
        "BaseURL": base_url,
        "LineupURL": f"{base_url}/lineup.json",
        "TunerCount": hdhomerun_emulator.tuner_count,
    }


@router.get("/lineup.json")
async def lineup_json(db: Session = Depends(get_db)):
    """HDHomeRun channel lineup"""
    try:
        base_url = get_advertised_base_url()

        items = db.query(Item).all()
        lineup = []
        channel_num = 1

        for item in items:
            filtered_m3u_path = f"/app/m3u_files/filtered_playlist_{item.id}.m3u"
            if not os.path.exists(filtered_m3u_path):
                continue

            try:
                with open(filtered_m3u_path, "r", encoding="utf-8") as f:
                    content = f.read()

                lines = content.split("\n")
                i = 0
                while i < len(lines):
                    line = lines[i]
                    if line.startswith("#EXTINF"):
                        # find next non-comment line = URL
                        j = i + 1
                        while j < len(lines) and lines[j].startswith("#"):
                            j += 1
                        if j < len(lines):
                            channel_name = "Unknown"
                            if "," in line:
                                channel_name = line.split(",", 1)[1].strip()

                            tvg_name = channel_name
                            attr_match = re.search(r'tvg-name="([^"]*)"', line)
                            if attr_match:
                                tvg_name = attr_match.group(1)

                            lineup.append(
                                {
                                    "GuideNumber": str(channel_num),
                                    "GuideName": tvg_name[:50],
                                    # ABSOLUTE URL so Plex doesn’t call itself
                                    "URL": f"{base_url}/auto_ff/{channel_num}",
                                }
                            )
                            channel_num += 1
                            i = j
                            continue
                    i += 1

            except Exception as e:
                logger.error(f"Error processing filtered M3U for item {item.id}: {e}")
                continue

        logger.info(f"HDHomeRun lineup: {len(lineup)} filtered channels")
        return JSONResponse(content=lineup)
    except Exception as e:
        logger.error(f"Error in lineup.json: {e}")
        return JSONResponse(content=[], status_code=500)


# ---- direct proxy endpoint (/auto/v{channel}) ------------------------------

@router.get("/auto/v{channel}")
async def stream_channel(channel: int, request: Request, db: Session = Depends(get_db)):
    """Stream a channel - with proper video player headers"""
    logger.info(f"🎬 Channel {channel} requested by {request.client.host}")

    items = db.query(Item).all()
    current_channel = 1
    stream_url = None
    channel_name = "Unknown"

    for item in items:
        filtered_m3u_path = f"/app/m3u_files/filtered_playlist_{item.id}.m3u"
        if not os.path.exists(filtered_m3u_path):
            continue

        try:
            with open(filtered_m3u_path, "r", encoding="utf-8") as f:
                content = f.read()

            lines = content.split("\n")
            i = 0
            while i < len(lines):
                line = lines[i]
                if line.startswith("#EXTINF"):
                    j = i + 1
                    while j < len(lines) and lines[j].startswith("#"):
                        j += 1
                    if j < len(lines):
                        if current_channel == channel:
                            if "," in line:
                                channel_name = line.split(",", 1)[1].strip()
                            stream_url = lines[j].strip()
                            break
                        current_channel += 1
                        i = j
                        continue
                i += 1

            if stream_url:
                break
        except Exception as e:
            logger.error(f"Error finding channel {channel}: {e}")
            continue

    if not stream_url:
        logger.error(f"Channel {channel} not found")
        raise HTTPException(status_code=404, detail=f"Channel {channel} not found")

    logger.info(f"Proxying: {channel_name} -> {stream_url}")

    try:
        headers = {
            "User-Agent": "VLC/3.0.18 LibVLC/3.0.18",
            "Accept": "*/*",
            "Range": "bytes=0-",
            "Connection": "keep-alive",
            "Referer": "http://localhost/",
        }
        if "user-agent" in request.headers:
            headers["User-Agent"] = request.headers["user-agent"]

        logger.info(f"Requesting stream with headers: {headers}")
        response = requests.get(stream_url, stream=True, timeout=30.0, headers=headers)

        if response.status_code not in (200, 206):
            logger.error(f"Stream returned HTTP {response.status_code}")
            raise HTTPException(status_code=502, detail=f"Stream error: HTTP {response.status_code}")

        content_type = response.headers.get("content-type", "video/mp2t")
        logger.info(f"Stream successful: {content_type}")

        def generate():
            try:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk
            except Exception as e:
                logger.error(f"Stream error: {e}")
            finally:
                try:
                    response.close()
                except Exception:
                    pass

        return StreamingResponse(
            generate(),
            media_type=content_type,
            headers={
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": "*",
                "Content-Type": content_type,
            },
        )

    except requests.exceptions.Timeout:
        logger.error(f"Timeout streaming from {stream_url}")
        raise HTTPException(status_code=504, detail="Stream timeout")
    except requests.exceptions.RequestException as e:
        logger.error(f"Stream error: {e}")
        raise HTTPException(status_code=502, detail=f"Stream error: {str(e)}")


# ---- diagnostics -----------------------------------------------------------

@router.get("/test-stream/{channel}")
async def test_stream(channel: int, db: Session = Depends(get_db)):
    mock_request = Request(scope={"type": "http", "client": ("127.0.0.1", 12345), "headers": []})
    return await stream_channel(channel, mock_request, db)


@router.get("/lineup_status.json")
async def lineup_status():
    return {"ScanInProgress": 0, "ScanPossible": 0, "Source": "Cable", "SourceList": ["Cable"]}


@router.get("/debug")
async def debug_info():
    base_url = get_advertised_base_url()
    return {"status": "running", "base_url": base_url, "test_url": f"{base_url}/test-stream/1"}


# ---- ffmpeg wrapper + endpoint --------------------------------------------

def _ffmpeg_stream(src_url: str) -> Iterator[bytes]:
    """
    Run ffmpeg to normalize SPS/PPS and emit a clean MPEG-TS for Plex.
    Video: H.264 (baseline/3.1); Audio: AAC stereo.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-fflags",
        "+genpts+discardcorrupt",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "10",
        "-i",
        src_url,  # <-- uses function arg (fixes NameError)
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "baseline",
        "-level",
        "3.1",
        "-g",
        "60",
        "-keyint_min",
        "60",
        "-sc_threshold",
        "0",
        "-c:a",
        "aac",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-b:a",
        "128k",
        "-f",
        "mpegts",
        "-mpegts_flags",
        "resend_headers+initial_discontinuity+pat_pmt_at_frames",
        "-muxpreload",
        "0",
        "-muxdelay",
        "0",
        "pipe:1",
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    try:
        while True:
            if proc.stdout is None:
                break
            chunk = proc.stdout.read(64 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


@router.get("/auto_ff/{chan}")
def auto_ff(chan: int, request: Request):
    """
    Normalizes /auto/v{chan} through ffmpeg for Plex.
    """
    # Use the app-internal endpoint as ffmpeg input (same container)
    upstream = f"http://127.0.0.1:5005/auto/v{chan}"
    logger.info(f"auto_ff: normalizing via ffmpeg for channel {chan} -> {upstream}")
    return StreamingResponse(_ffmpeg_stream(upstream), media_type="video/mp2t")
