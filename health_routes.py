"""
health_routes.py — Stream tester UI and health-check endpoint.
"""
import logging
import time

import requests
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

import config
from hdhomerun_routes import get_active_stream_count, get_active_streams
from models import Item, get_db

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Stream Tester
# ---------------------------------------------------------------------------

@router.get("/stream_test", response_class=HTMLResponse)
async def stream_test_page(request: Request):
    template = templates.get_template("stream_test.html")
    rendered = template.render({"request": request})
    return HTMLResponse(content=rendered)


@router.post("/api/stream_test")
async def api_stream_test(
    username: str = Form(...),
    password: str = Form(...),
    server_url: str = Form(...),
    tvg_id: str = Form(...),
):
    server_url = server_url.rstrip("/")
    stream_url = f"{server_url}/live/{username}/{password}/{tvg_id}.ts"
    headers = {
        "User-Agent": "VLC/3.0.18 LibVLC/3.0.18",
        "Accept": "*/*",
        "Connection": "close",
    }
    try:
        resp = requests.get(stream_url, stream=True, timeout=10, headers=headers, allow_redirects=True)
        resp.raise_for_status()
        chunk = next(resp.iter_content(chunk_size=4096), None)
        resp.close()
        if chunk:
            return {"success": True, "stream_url": stream_url}
        return {
            "success": False,
            "stream_url": stream_url,
            "error": "Stream connected but returned no data",
            "detail": f"HTTP {resp.status_code} — server responded but sent 0 bytes",
        }
    except requests.exceptions.HTTPError as e:
        reason = e.response.reason or "Unknown"
        body_preview = ""
        try:
            body_preview = e.response.text[:300].strip()
        except Exception:
            pass
        return {
            "success": False,
            "stream_url": stream_url,
            "error": f"HTTP {e.response.status_code}: {reason}",
            "detail": body_preview or None,
            "headers": dict(e.response.headers),
        }
    except requests.exceptions.ConnectionError as e:
        return {
            "success": False,
            "stream_url": stream_url,
            "error": "Connection failed",
            "detail": str(e),
        }
    except requests.exceptions.Timeout:
        return {
            "success": False,
            "stream_url": stream_url,
            "error": "Connection timed out (10s)",
            "detail": f"No response from server within 10 seconds: {stream_url}",
        }
    except Exception as e:
        return {
            "success": False,
            "stream_url": stream_url,
            "error": str(e),
            "detail": type(e).__name__,
        }


# ---------------------------------------------------------------------------
# Health check endpoint (for Uptime Kuma, Home Assistant, etc.)
# ---------------------------------------------------------------------------

@router.get("/api/health")
async def api_health(
    db: Session = Depends(get_db),
    item_id: int = None,
    tvg_id: str = None,
):
    active = get_active_stream_count()

    if active > 0:
        sessions = get_active_streams()
        now = time.time()
        sessions_out = [
            {
                "channel": s["channel"],
                "client_ip": s["client_ip"],
                "user_agent": s["user_agent"],
                "elapsed_s": round(now - s["started_at"]),
                "bytes_sent": s["bytes_sent"],
            }
            for s in sessions
        ]
        return {"status": "skipped", "reason": "stream_active", "active_streams": active, "sessions": sessions_out}

    tvg_id = tvg_id or config.HEALTH_CHECK_TVG_ID
    if not tvg_id:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "reason": "no_tvg_id_configured",
                     "detail": "Set HEALTH_CHECK_TVG_ID in .env or pass ?tvg_id= as a query param"},
        )

    if item_id is not None:
        item = db.query(Item).filter(Item.id == item_id).first()
    else:
        item = db.query(Item).first()

    if not item:
        return JSONResponse(
            status_code=404,
            content={"status": "error", "reason": "no_item_found",
                     "detail": "No IPTV configuration found in database"},
        )

    server_url = item.server_url.rstrip("/")
    stream_url = f"{server_url}/live/{item.username}/{item.user_pass}/{tvg_id}.ts"
    headers = {
        "User-Agent": "VLC/3.0.18 LibVLC/3.0.18",
        "Accept": "*/*",
        "Connection": "close",
    }

    t0 = time.monotonic()
    resp = None
    try:
        resp = requests.get(stream_url, stream=True, timeout=10, headers=headers, allow_redirects=True)
        resp.raise_for_status()
        chunk = next(resp.iter_content(chunk_size=4096), None)
        latency_ms = int((time.monotonic() - t0) * 1000)
        if chunk:
            return {"status": "ok", "latency_ms": latency_ms, "item": item.name, "active_streams": 0}
        return JSONResponse(
            status_code=503,
            content={"status": "down", "error": "Stream connected but returned no data",
                     "latency_ms": latency_ms, "item": item.name, "active_streams": 0},
        )
    except requests.exceptions.HTTPError as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return JSONResponse(
            status_code=503,
            content={"status": "down",
                     "error": f"HTTP {e.response.status_code}: {e.response.reason or 'Unknown'}",
                     "latency_ms": latency_ms, "item": item.name, "active_streams": 0},
        )
    except requests.exceptions.Timeout:
        return JSONResponse(
            status_code=503,
            content={"status": "down", "error": "Connection timed out (10s)",
                     "item": item.name, "active_streams": 0},
        )
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"status": "down", "error": str(e), "item": item.name, "active_streams": 0},
        )
    finally:
        if resp is not None:
            resp.close()
