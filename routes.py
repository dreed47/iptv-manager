from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates
import time
from sqlalchemy.orm import Session
from models import get_db, Item
from services import create_item, update_item, get_item_context
import config
import logging
import os
import re
from hdhomerun_routes import hdhomerun_emulator, get_active_streams, kill_stream, KILL_BLOCK_SECONDS
import urllib.parse
import json
from epg_manager import get_epg as _refresh_epg
from m3u_service import do_fetch_m3u, build_filter_config, apply_m3u_filter

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

def get_base_url(request: Request) -> str:
    return config.ADVERTISED_BASE_URL

@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db), error: str = None, success: str = None):
    base_url = get_base_url(request)
    item = get_item_context(db, base_url, config.M3U_DIR)
    ssdp_disabled_by_env = hdhomerun_emulator.is_env_disabled()
    start = time.perf_counter()
    template = templates.get_template("dashboard.html")
    rendered = template.render({
        "request": request,
        "item": item,
        "error": error,
        "success": success,
        "base_url": base_url,
        "hdhr_running": hdhomerun_emulator.is_running(),
        "can_enable_ssdp": not ssdp_disabled_by_env,
        "friendly_name": config.HDHR_FRIENDLY_NAME,
        "active_page": "dashboard",
    })
    logger.debug(f"Template render duration: {time.perf_counter() - start:.3f}s")
    return HTMLResponse(content=rendered)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db), error: str = None, success: str = None):
    base_url = get_base_url(request)
    item = get_item_context(db, base_url, config.M3U_DIR)
    start = time.perf_counter()
    template = templates.get_template("settings.html")
    rendered = template.render({
        "request": request,
        "item": item,
        "friendly_name": config.HDHR_FRIENDLY_NAME,
        "active_page": "settings",
        "allow_full_m3u_download": config.ALLOW_FULL_M3U_DOWNLOAD,
        "error": error,
        "success": success,
    })
    logger.debug(f"Template render duration: {time.perf_counter() - start:.3f}s")
    return HTMLResponse(content=rendered)


@router.post("/settings", response_class=RedirectResponse)
async def handle_settings_form(
    request: Request,
    name: str = Form(...),
    server_url: str = Form(...),
    username: str = Form(...),
    user_pass: str = Form(...),
    m3u_refresh_hours: int = Form(0),
    max_sessions: int = Form(1),
    db: Session = Depends(get_db),
):
    existing = db.query(Item).first()
    if existing:
        result = update_item(db, existing.id, name, server_url, username, user_pass, None, None, None, m3u_refresh_hours=m3u_refresh_hours, max_sessions=max_sessions)
    else:
        result = create_item(db, name, server_url, username, user_pass, None, None, None, max_sessions=max_sessions)
        if result:
            update_item(db, result.id, None, None, None, None, None, None, None, m3u_refresh_hours, max_sessions=max_sessions)
    if not result:
        return RedirectResponse(url="/settings?error=Failed to save provider", status_code=303)
    return RedirectResponse(url="/settings?success=Provider saved successfully", status_code=303)

@router.get("/settings/test_connection", response_class=JSONResponse)
async def test_connection(request: Request, db: Session = Depends(get_db)):
    import requests as _requests
    import asyncio
    item = db.query(Item).first()
    if not item:
        return JSONResponse({"ok": False, "error": "No provider configured"})
    url = f"{item.server_url.rstrip('/')}/player_api.php"
    params = {"username": item.username, "password": item.user_pass}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"}
    def _do_test():
        resp = _requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()
    try:
        data = await asyncio.to_thread(_do_test)
        if data.get("user_info", {}).get("auth") == 1:
            import datetime
            status = data["user_info"].get("status", "")
            exp_ts = data["user_info"].get("exp_date", "")
            try:
                exp = datetime.datetime.fromtimestamp(int(exp_ts)).strftime("%b %d, %Y")
            except (ValueError, TypeError):
                exp = exp_ts
            item.provider_status = status
            item.provider_exp_date = exp
            db.commit()
            return JSONResponse({"ok": True, "message": f"Connected — status: {status}, expires: {exp}"})
        return JSONResponse({"ok": False, "error": "Auth failed — check username/password"})
    except _requests.exceptions.ConnectionError as e:
        return JSONResponse({"ok": False, "error": f"Could not connect to {url}: {e}"})
    except _requests.exceptions.Timeout:
        return JSONResponse({"ok": False, "error": f"Connection timed out after 10s: {url}"})
    except _requests.exceptions.HTTPError as e:
        return JSONResponse({"ok": False, "error": f"Server returned HTTP {e.response.status_code}: {url}"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"})


@router.get("/api/active_streams", response_class=JSONResponse)
async def api_active_streams(db: Session = Depends(get_db)):
    import math
    streams = get_active_streams()
    item = db.query(Item).first()
    max_sessions = int(item.max_sessions) if item and item.max_sessions is not None else 1
    out = []
    now = time.time()
    for s in streams:
        elapsed = int(now - s.get("started_at", now))
        hours, rem = divmod(elapsed, 3600)
        mins, secs = divmod(rem, 60)
        duration = f"{hours}h {mins}m" if hours else f"{mins}m {secs}s"
        mb = s.get("bytes_sent", 0) / (1024 * 1024)
        channel_num = s.get("channel", "?")
        channel_name = s.get("channel_name", "")
        channel_display = f"{channel_num} — {channel_name}" if channel_name and channel_name != channel_num else channel_num
        out.append({
            "session_id": s.get("session_id", ""),
            "channel": channel_display,
            "client_ip": s.get("client_ip", "?"),
            "duration": duration,
            "mb_sent": round(mb, 1),
        })
    return JSONResponse({"streams": out, "max_sessions": max_sessions})


@router.post("/api/streams/{session_id}/kill", response_class=JSONResponse)
async def kill_stream_api(session_id: str):
    ok = kill_stream(session_id)
    if not ok:
        return JSONResponse({"ok": False, "error": "Session not found"}, status_code=404)
    return JSONResponse({"ok": True, "block_seconds": KILL_BLOCK_SECONDS})


_plex_bg_tasks: set = set()
_pending_orphan_task: "asyncio.Task | None" = None


@router.post("/api/plex/webhook")
async def plex_webhook(payload: str = Form(None)):
    global _pending_orphan_task
    if not payload:
        return JSONResponse({"ok": True})
    try:
        data = json.loads(payload)
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)

    import asyncio
    event = data.get("event", "")
    is_live = bool(data.get("Metadata", {}).get("live"))

    if is_live and event in ("media.play", "media.resume"):
        if _pending_orphan_task and not _pending_orphan_task.done():
            _pending_orphan_task.cancel()
            logger.info("Plex webhook: cancelled orphan check — new live session started")
        return JSONResponse({"ok": True})

    if event != "media.stop" or not is_live:
        return JSONResponse({"ok": True})

    player = data.get("Player", {}).get("title", "?")
    logger.info(f"Plex webhook: media.stop live TV — player='{player}'")
    logger.debug(f"Plex webhook metadata: {json.dumps(data.get('Metadata', {}))}")
    if _pending_orphan_task and not _pending_orphan_task.done():
        _pending_orphan_task.cancel()
    task = asyncio.create_task(_release_orphan_streams())
    _pending_orphan_task = task
    _plex_bg_tasks.add(task)
    task.add_done_callback(_plex_bg_tasks.discard)
    return JSONResponse({"ok": True})


async def _release_orphan_streams():
    import asyncio, requests as _req
    logger.info("Plex: orphan check task started")
    await asyncio.sleep(3)
    logger.info("Plex: orphan check running after sleep")
    if not config.PLEX_URL or not config.PLEX_TOKEN:
        logger.warning("Plex: PLEX_URL or PLEX_TOKEN not set, skipping")
        return
    try:
        resp = await asyncio.to_thread(
            lambda: _req.get(
                f"{config.PLEX_URL}/status/sessions",
                headers={"X-Plex-Token": config.PLEX_TOKEN, "Accept": "application/json"},
                timeout=5,
            )
        )
        resp.raise_for_status()
        plex_sessions = resp.json().get("MediaContainer", {}).get("Metadata") or []
        plex_live_count = sum(1 for s in plex_sessions if s.get("live"))
        our_streams = get_active_streams()
        logger.info(f"Plex session check: {plex_live_count} Plex live session(s), {len(our_streams)} active stream(s)")
        if len(our_streams) > plex_live_count:
            # Kill oldest streams first until counts match
            to_kill = sorted(our_streams, key=lambda s: s.get("started_at", 0))
            for s in to_kill[:len(our_streams) - plex_live_count]:
                kill_stream(s["session_id"], block_ip=False)
                logger.info(
                    f"Plex webhook: released orphan stream {s['session_id']} "
                    f"channel='{s.get('channel_name', '?')}'"
                )
    except Exception as exc:
        logger.warning(f"Plex session check failed: {exc}")


@router.get("/api/logs", response_class=JSONResponse)
async def api_logs(level: str = "", since: float = 0):
    from main import get_log_buffer
    entries = get_log_buffer()
    if level:
        lvl = level.upper()
        entries = [e for e in entries if e["level"] == lvl]
    if since:
        entries = [e for e in entries if e["ts"] >= since]
    return JSONResponse({"logs": entries})


@router.get("/tools/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    start = time.perf_counter()
    template = templates.get_template("logs.html")
    rendered = template.render({"request": request, "active_page": "tools"})
    logger.debug(f"Template render duration: {time.perf_counter() - start:.3f}s")
    return HTMLResponse(content=rendered)


@router.post("/set_refresh_interval")
async def set_refresh_interval(item_id: int = Form(...), m3u_refresh_hours: int = Form(0), db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        return JSONResponse({"ok": False, "error": "Item not found"}, status_code=404)
    item.m3u_refresh_hours = m3u_refresh_hours
    db.commit()
    db.refresh(item)
    logger.debug(f"Set m3u_refresh_hours={item.m3u_refresh_hours} for item {item_id}")
    return JSONResponse({"ok": True, "m3u_refresh_hours": item.m3u_refresh_hours})


@router.post("/generate_m3u", response_class=RedirectResponse)
async def generate_m3u(background_tasks: BackgroundTasks, item_id: int = Form(...), db: Session = Depends(get_db)):
    try:
        ok, msg, _ = do_fetch_m3u(item_id, db)
        if not ok:
            return RedirectResponse(url=f"/settings?error={urllib.parse.quote(msg)}", status_code=303)
        background_tasks.add_task(_refresh_epg, True)
        logger.debug("EPG rebuild queued in background after M3U save")
        return RedirectResponse(url=f"/settings?success={urllib.parse.quote(msg)}", status_code=303)
    except Exception as e:
        logger.error(f"Failed to generate M3U for item {item_id}: {e}")
        return RedirectResponse(url=f"/settings?error=Failed to fetch M3U: {urllib.parse.quote(str(e))}", status_code=303)

@router.post("/generate_filtered_m3u", response_class=RedirectResponse)
async def generate_filtered_m3u(background_tasks: BackgroundTasks, item_id: int = Form(...), db: Session = Depends(get_db)):
    try:
        item = db.query(Item).filter(Item.id == item_id).first()
        if not item:
            logger.warning(f"Item with id {item_id} not found for filtered M3U generation")
            return RedirectResponse(url="/hdhomerun?error=Item not found", status_code=303)

        m3u_path = os.path.join(config.M3U_DIR, f"xtream_playlist_{item_id}.m3u")
        if not os.path.exists(m3u_path):
            logger.warning(f"M3U file not found for item {item_id} at {m3u_path}")
            return RedirectResponse(url="/hdhomerun?error=M3U file not found, fetch M3U first", status_code=303)

        with open(m3u_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()

        languages, includes_map, raw_includes, excludes, has_wildcard = build_filter_config(item)
        logger.debug(
            f"Filtering item {item_id}: languages={languages} includes={raw_includes} "
            f"excludes={excludes} wildcard={has_wildcard}"
        )

        filtered_content, num_records, input_record_count = apply_m3u_filter(
            lines, languages, includes_map, excludes, has_wildcard
        )

        if num_records == 0:
            logger.warning(f"No records matched filter for item {item_id}")
            return RedirectResponse(url="/hdhomerun?error=No records matched the filter criteria.", status_code=303)

        os.makedirs(config.M3U_DIR, exist_ok=True)
        filtered_path = os.path.join(config.M3U_DIR, f"filtered_playlist_{item_id}.m3u")
        tmp_path = filtered_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(filtered_content)
        os.replace(tmp_path, filtered_path)

        total_lines = len(filtered_content.splitlines())
        logger.info(
            f"Filtered M3U for item {item_id}: input={input_record_count} kept={num_records} lines={total_lines}"
        )
        success_msg = urllib.parse.quote(
            f"Filtered {num_records} of {input_record_count} records ({total_lines} lines)"
        )
        background_tasks.add_task(_refresh_epg, True)
        logger.debug("EPG rebuild queued in background after filtered M3U save")
        return RedirectResponse(url=f"/hdhomerun?success={success_msg}", status_code=303)

    except Exception as e:
        logger.error(f"Failed to generate filtered M3U for item {item_id}: {str(e)}")
        return RedirectResponse(url=f"/hdhomerun?error=Failed to save filtered M3U file: {str(e)}", status_code=303)

@router.get("/download_m3u/{item_id}", response_class=FileResponse)
async def download_m3u(item_id: int, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    
    file_path = os.path.join(config.M3U_DIR, f"xtream_playlist_{item_id}.m3u")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="M3U file not found")
    
    return FileResponse(file_path, filename=f"xtream_playlist_{item.name}.m3u")

@router.get("/download_filtered_m3u/{item_id}", response_class=FileResponse)
async def download_filtered_m3u(item_id: int, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    
    file_path = os.path.join(config.M3U_DIR, f"filtered_playlist_{item_id}.m3u")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Filtered M3U file not found")
    
    return FileResponse(file_path, filename=f"filtered_playlist_{item.name}.m3u")

@router.get("/stream_filtered_m3u/{item_id}")
async def stream_filtered_m3u(item_id: int, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    
    file_path = os.path.join(config.M3U_DIR, f"filtered_playlist_{item_id}.m3u")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Filtered M3U file not found")
    
    # Return the M3U content directly with proper M3U headers
    with open(file_path, "r", encoding="utf-8") as f:
        m3u_content = f.read()
    
    return Response(
        content=m3u_content,
        media_type="application/x-mpegurl",
        headers={
            "Content-Disposition": f'attachment; filename="filtered_playlist_{item.name}.m3u"',
            "Access-Control-Allow-Origin": "*"
        }
    )


# ---------------------------------------------------------------------------
# Direct stream player (used by M3U browser play button)
# ---------------------------------------------------------------------------

@router.get("/stream_player", response_class=HTMLResponse)
async def stream_player(request: Request, url: str = "", name: str = "Stream"):
    """Open a direct stream URL in the player page."""
    import urllib.parse as _up
    stash_kb    = config.PLAYER_STASH_KB
    latency_max = config.PLAYER_LATENCY_MAX
    latency_min = config.PLAYER_LATENCY_MIN
    template = templates.get_template("player.html")
    rendered = template.render({
        "request": request,
        "channel_number": "",
        "channel_name": name,
        "channel_name_encoded": _up.quote(name),
        "stream_url_encoded": _up.quote(url, safe=""),
        "stream_url_json": json.dumps(url),
        "vlc_href": url,
        "stash_kb": stash_kb,
        "latency_max": latency_max,
        "latency_min": latency_min,
    })
    return HTMLResponse(content=rendered)


# ---------------------------------------------------------------------------
# Generic URL proxy — used by the player for direct MP4/VOD URLs that the
# browser can't fetch directly due to CORS or provider restrictions.
# ---------------------------------------------------------------------------

@router.get("/api/proxy_url")
async def proxy_url(request: Request, url: str):
    import requests as _req
    from fastapi.responses import StreamingResponse as _SR

    proxy_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
    }
    range_header = request.headers.get("range")
    proxy_headers["Range"] = range_header or "bytes=0-"

    try:
        resp = _req.get(url, headers=proxy_headers, stream=True, timeout=(10, 120), allow_redirects=True)
        resp.raise_for_status()
    except _req.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 502
        return JSONResponse({"error": f"Upstream {status}"}, status_code=502)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    forward_headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
    for hdr in ("Content-Type", "Content-Range"):
        val = resp.headers.get(hdr)
        if val:
            forward_headers[hdr] = val

    client_status = 206 if resp.status_code == 206 else 200

    def _stream():
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                yield chunk

    return _SR(_stream(), status_code=client_status, headers=forward_headers,
               media_type=forward_headers.get("Content-Type", "video/mp4"))


# ---------------------------------------------------------------------------
# Full M3U browser
# ---------------------------------------------------------------------------

@router.get("/m3u_browser/{item_id}", response_class=HTMLResponse)
async def m3u_browser(item_id: int, request: Request, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    file_path = os.path.join(config.M3U_DIR, f"xtream_playlist_{item_id}.m3u")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Full M3U not fetched yet — click Fetch M3U first")
    template = templates.get_template("m3u_browser.html")
    rendered = template.render({
        "request": request,
        "item_id": item_id,
        "item_name": item.name,
    })
    return HTMLResponse(content=rendered)


@router.get("/m3u_browser_data/{item_id}")
async def m3u_browser_data(
    item_id: int,
    db: Session = Depends(get_db),
    search: str = "",
    group: str = "",
    prefix: str = "",
    page: int = 1,
    per_page: int = 100,
):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    file_path = os.path.join(config.M3U_DIR, f"xtream_playlist_{item_id}.m3u")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Full M3U not found")

    channels = []
    groups: set = set()
    prefixes: dict = {}

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()

    i = 1 if (lines and lines[0].strip() == "#EXTM3U") else 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF") and i + 1 < len(lines):
            extinf = line
            url = lines[i + 1]
            attrs = {}
            display_name = ""
            if "," in extinf:
                attr_part, display_name = extinf.split(",", 1)
                for k, v in re.findall(r'(\S+?)="([^"]*)"', attr_part):
                    attrs[k.lower()] = v
            display_name = display_name.strip()
            tvg_name = attrs.get("tvg-name", "").strip()
            group_title = attrs.get("group-title", "").strip()

            # Detect provider prefix  e.g. "SLING: ESPN" -> "SLING:", "EN - BBC" -> "EN -"
            src = tvg_name or display_name
            ch_prefix = ""
            if ":" in src:
                candidate = src.split(":")[0].strip()
                if 1 < len(candidate) <= 15 and not any(c.isdigit() for c in candidate):
                    ch_prefix = candidate + ":"
            elif " - " in src:
                candidate = src.split(" - ")[0].strip()
                if 1 < len(candidate) <= 6:
                    ch_prefix = candidate + " -"

            if group_title:
                groups.add(group_title)
            if ch_prefix:
                prefixes[ch_prefix] = prefixes.get(ch_prefix, 0) + 1

            channels.append({
                "name": display_name,
                "tvg_name": tvg_name,
                "group": group_title,
                "prefix": ch_prefix,
                "url": url,
            })
            i += 2
        else:
            i += 1

    # Only include prefixes that appear on at least 5 channels (avoids one-off channel name colons)
    MIN_PREFIX_COUNT = 5
    valid_prefixes = {p for p, count in prefixes.items() if count >= MIN_PREFIX_COUNT}

    # Clear prefix on channels whose detected prefix isn't a real provider
    for ch in channels:
        if ch["prefix"] and ch["prefix"] not in valid_prefixes:
            ch["prefix"] = ""

    # Apply filters
    filtered = channels
    if search:
        s = search.lower()
        filtered = [c for c in filtered if s in c["name"].lower() or s in c["tvg_name"].lower()]
    if group:
        filtered = [c for c in filtered if c["group"] == group]
    if prefix:
        filtered = [c for c in filtered if c["prefix"] == prefix]

    total = len(filtered)
    per_page = max(10, min(per_page, 500))
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page

    return {
        "channels": filtered[start:start + per_page],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "groups": sorted(groups),
        "prefixes": sorted(valid_prefixes),
    }


@router.get("/hdhomerun", response_class=HTMLResponse)
async def hdhomerun_page(request: Request, db: Session = Depends(get_db), error: str = None, success: str = None):
    base_url = get_base_url(request)
    item = get_item_context(db, base_url, config.M3U_DIR)
    ssdp_disabled_by_env = hdhomerun_emulator.is_env_disabled()
    start = time.perf_counter()
    template = templates.get_template("hdhomerun.html")
    rendered = template.render({
        "request": request,
        "item": item,
        "friendly_name": config.HDHR_FRIENDLY_NAME,
        "active_page": "hdhomerun",
        "hdhr_running": hdhomerun_emulator.is_running(),
        "can_enable_ssdp": not ssdp_disabled_by_env,
        "allow_full_m3u_download": config.ALLOW_FULL_M3U_DOWNLOAD,
        "error": error,
        "success": success,
    })
    logger.debug(f"Template render duration: {time.perf_counter() - start:.3f}s")
    return HTMLResponse(content=rendered)


@router.post("/hdhomerun", response_class=RedirectResponse)
async def handle_hdhomerun_form(
    request: Request,
    background_tasks: BackgroundTasks,
    save_filters: str = Form(None),
    item_id: int = Form(None),
    new_includes: str = Form(None),
    m3u_refresh_hours: int = Form(None),
    db: Session = Depends(get_db),
):
    if save_filters and item_id:
        if new_includes and '\n' in new_includes:
            new_includes = ','.join([inc.strip() for inc in new_includes.split('\n') if inc.strip()])
        result = update_item(db, item_id, None, None, None, None, None, new_includes, None, m3u_refresh_hours)
        if not result:
            return RedirectResponse(url="/hdhomerun?error=Failed to save filters", status_code=303)

        m3u_path = os.path.join(config.M3U_DIR, f"xtream_playlist_{item_id}.m3u")
        if not os.path.exists(m3u_path):
            return RedirectResponse(
                url="/hdhomerun?success=Filters saved — fetch M3U first to generate filtered playlist",
                status_code=303,
            )

        try:
            item = db.query(Item).filter(Item.id == item_id).first()
            with open(m3u_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()

            languages, includes_map, raw_includes, excludes, has_wildcard = build_filter_config(item)
            filtered_content, num_records, input_record_count = apply_m3u_filter(
                lines, languages, includes_map, excludes, has_wildcard
            )

            if num_records == 0:
                return RedirectResponse(
                    url="/hdhomerun?error=Filters saved but no channels matched — check your filter list",
                    status_code=303,
                )

            os.makedirs(config.M3U_DIR, exist_ok=True)
            filtered_path = os.path.join(config.M3U_DIR, f"filtered_playlist_{item_id}.m3u")
            tmp_path = filtered_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(filtered_content)
            os.replace(tmp_path, filtered_path)

            background_tasks.add_task(_refresh_epg, True)
            success_msg = urllib.parse.quote(
                f"Filters saved — {num_records} of {input_record_count} channels matched"
            )
            return RedirectResponse(url=f"/hdhomerun?success={success_msg}", status_code=303)
        except Exception as e:
            logger.error(f"Failed to generate filtered M3U after save: {e}")
            return RedirectResponse(
                url=f"/hdhomerun?error=Filters saved but filtering failed: {urllib.parse.quote(str(e))}",
                status_code=303,
            )

    return RedirectResponse(url="/hdhomerun", status_code=303)


@router.get("/xtream", response_class=HTMLResponse)
async def xtream_page(request: Request, db: Session = Depends(get_db), error: str = None, success: str = None):
    base_url = get_base_url(request)
    item = get_item_context(db, base_url, config.M3U_DIR)
    start = time.perf_counter()
    template = templates.get_template("xtream.html")
    rendered = template.render({
        "request": request,
        "item": item,
        "friendly_name": config.HDHR_FRIENDLY_NAME,
        "active_page": "xtream",
        "iptv_username": config.IPTV_USERNAME,
        "iptv_password": config.IPTV_PASSWORD,
        "base_url": base_url,
        "error": error,
        "success": success,
    })
    logger.debug(f"Template render duration: {time.perf_counter() - start:.3f}s")
    return HTMLResponse(content=rendered)


@router.post("/xtream", response_class=RedirectResponse)
async def handle_xtream_form(
    request: Request,
    save_credentials: str = Form(None),
    save_xtream_filters: str = Form(None),
    iptv_username: str = Form(None),
    iptv_password: str = Form(None),
    item_id: int = Form(None),
    new_xtream_includes: str = Form(None),
    db: Session = Depends(get_db),
):
    if save_credentials:
        env_file = os.path.join(os.getcwd(), '.env')
        try:
            env_lines = []
            if os.path.exists(env_file):
                with open(env_file, 'r') as f:
                    env_lines = f.readlines()
            updated_user = updated_pass = False
            for i, line in enumerate(env_lines):
                if line.startswith('IPTV_USERNAME='):
                    env_lines[i] = f'IPTV_USERNAME={iptv_username}\n'
                    updated_user = True
                elif line.startswith('IPTV_PASSWORD='):
                    env_lines[i] = f'IPTV_PASSWORD={iptv_password}\n'
                    updated_pass = True
            if not updated_user:
                env_lines.append(f'IPTV_USERNAME={iptv_username}\n')
            if not updated_pass:
                env_lines.append(f'IPTV_PASSWORD={iptv_password}\n')
            tmp_env = env_file + ".tmp"
            with open(tmp_env, 'w') as f:
                f.writelines(env_lines)
            os.replace(tmp_env, env_file)
            config.IPTV_USERNAME = iptv_username
            config.IPTV_PASSWORD = iptv_password
            return RedirectResponse(url="/xtream?success=Credentials saved successfully", status_code=303)
        except Exception as e:
            logger.error(f"Failed to save Xtream credentials: {e}")
            return RedirectResponse(url="/xtream?error=Failed to save credentials", status_code=303)

    elif save_xtream_filters and item_id:
        if new_xtream_includes and '\n' in new_xtream_includes:
            new_xtream_includes = ','.join([x.strip() for x in new_xtream_includes.split('\n') if x.strip()])
        result = update_item(db, item_id, None, None, None, None, None, None, None, new_xtream_includes, None)
        if not result:
            return RedirectResponse(url="/xtream?error=Failed to save Xtream filters", status_code=303)
        return RedirectResponse(url="/xtream?success=Xtream filters saved successfully", status_code=303)

    return RedirectResponse(url="/xtream", status_code=303)


# ---------------------------------------------------------------------------
# VPN endpoints
# ---------------------------------------------------------------------------

@router.post("/vpn/settings", response_class=RedirectResponse)
async def save_vpn_settings(
    vpn_config: str = Form(""),
    vpn_username: str = Form(""),
    vpn_password: str = Form(""),
    db: Session = Depends(get_db),
):
    item = db.query(Item).first()
    if not item:
        return RedirectResponse(url="/settings?error=No provider configured", status_code=303)
    if vpn_config.strip():
        item.vpn_config = vpn_config.strip()
    if vpn_username.strip():
        item.vpn_username = vpn_username.strip()
    if vpn_password.strip():
        item.vpn_password = vpn_password.strip()
    db.commit()
    return RedirectResponse(url="/settings?success=VPN settings saved", status_code=303)


@router.post("/vpn/enable", response_class=RedirectResponse)
async def vpn_enable(request: Request, db: Session = Depends(get_db)):
    import vpn_manager
    import asyncio
    next_url = (await request.form()).get("next", "/settings")
    item = db.query(Item).first()
    if not item:
        return RedirectResponse(url="/settings?error=No provider configured", status_code=303)
    if not (item.vpn_config and item.vpn_username and item.vpn_password):
        return RedirectResponse(
            url=f"{next_url}?error=VPN settings incomplete — save .ovpn config and credentials first",
            status_code=303,
        )
    ok, msg = await asyncio.to_thread(
        vpn_manager.start_vpn, item.vpn_config, item.vpn_username, item.vpn_password
    )
    if ok:
        item.vpn_enabled = True
        db.commit()
        return RedirectResponse(url=f"{next_url}?success=VPN connected", status_code=303)
    return RedirectResponse(
        url=f"{next_url}?error={urllib.parse.quote('VPN failed: ' + msg)}", status_code=303
    )


@router.post("/vpn/disable", response_class=RedirectResponse)
async def vpn_disable(request: Request, db: Session = Depends(get_db)):
    import vpn_manager
    import asyncio
    next_url = (await request.form()).get("next", "/settings")
    ok, msg = await asyncio.to_thread(vpn_manager.stop_vpn)
    item = db.query(Item).first()
    if item:
        item.vpn_enabled = False
        db.commit()
    if ok:
        return RedirectResponse(url=f"{next_url}?success=VPN disconnected", status_code=303)
    return RedirectResponse(
        url=f"{next_url}?error={urllib.parse.quote('VPN stop failed: ' + msg)}", status_code=303
    )


@router.get("/vpn/status", response_class=JSONResponse)
async def vpn_status():
    import vpn_manager
    import asyncio
    status = await asyncio.to_thread(vpn_manager.get_vpn_status)
    if status["running"]:
        status["external_ip"] = await asyncio.to_thread(vpn_manager.get_external_ip)
    else:
        status["external_ip"] = None
    return JSONResponse(status)


@router.get("/settings/test_vpn", response_class=JSONResponse)
async def test_vpn():
    import vpn_manager
    import asyncio
    status, ip = await asyncio.gather(
        asyncio.to_thread(vpn_manager.get_vpn_status),
        asyncio.to_thread(vpn_manager.get_external_ip),
    )
    return JSONResponse({
        "ip": ip,
        "vpn_running": status["running"],
        "interface": status.get("interface"),
        "error": None if ip else "Could not reach IP check service (api.ipify.org)",
    })


@router.get("/tools", response_class=HTMLResponse)
async def tools_page(request: Request, db: Session = Depends(get_db), error: str = None, success: str = None):
    base_url = get_base_url(request)
    item = get_item_context(db, base_url, config.M3U_DIR)
    start = time.perf_counter()
    template = templates.get_template("tools.html")
    rendered = template.render({
        "request": request,
        "item": item,
        "friendly_name": config.HDHR_FRIENDLY_NAME,
        "active_page": "tools",
        "base_url": base_url,
        "hdhr_running": hdhomerun_emulator.is_running(),
        "error": error,
        "success": success,
    })
    logger.debug(f"Template render duration: {time.perf_counter() - start:.3f}s")
    return HTMLResponse(content=rendered)


@router.post("/mqtt/settings", response_class=RedirectResponse)
async def save_mqtt_settings(
    request: Request,
    db: Session = Depends(get_db),
    mqtt_enabled: str = Form(""),
    mqtt_host: str = Form(""),
    mqtt_port: int = Form(1883),
    mqtt_username: str = Form(""),
    mqtt_password: str = Form(""),
    mqtt_topic_prefix: str = Form("iptv-manager"),
    mqtt_device_name: str = Form("IPTV Manager"),
):
    import mqtt_manager as _mqtt
    item = db.query(Item).first()
    if not item:
        return RedirectResponse(url="/tools?error=No provider configured", status_code=303)

    enabled = mqtt_enabled.lower() in ("1", "true", "on", "yes")

    item.mqtt_enabled = enabled
    item.mqtt_host = mqtt_host.strip() or None
    item.mqtt_port = mqtt_port
    item.mqtt_username = mqtt_username.strip() or None
    if mqtt_password:
        item.mqtt_password = mqtt_password
    item.mqtt_topic_prefix = mqtt_topic_prefix.strip() or "iptv-manager"
    item.mqtt_device_name = mqtt_device_name.strip() or "IPTV Manager"
    db.commit()

    cfg = {
        "mqtt_host": item.mqtt_host,
        "mqtt_port": item.mqtt_port,
        "mqtt_username": item.mqtt_username,
        "mqtt_password": item.mqtt_password,
        "mqtt_topic_prefix": item.mqtt_topic_prefix,
        "mqtt_device_name": item.mqtt_device_name,
    }
    if enabled and item.mqtt_host:
        ok, msg = _mqtt.start_mqtt(cfg)
        if not ok:
            return RedirectResponse(url=f"/tools?error={urllib.parse.quote('MQTT saved but connect failed: ' + msg)}", status_code=303)
    else:
        _mqtt.stop_mqtt()

    return RedirectResponse(url="/tools?success=MQTT settings saved", status_code=303)


@router.get("/mqtt/status", response_class=JSONResponse)
async def mqtt_status():
    import mqtt_manager as _mqtt
    return JSONResponse(_mqtt.get_mqtt_status())


@router.post("/mqtt/ha_discovery", response_class=JSONResponse)
async def mqtt_ha_discovery():
    import mqtt_manager as _mqtt
    status = _mqtt.get_mqtt_status()
    if not status.get("connected"):
        return JSONResponse({"ok": False, "error": "MQTT not connected — connect first"})
    mgr = _mqtt._manager
    if mgr is None:
        return JSONResponse({"ok": False, "error": "No active MQTT manager"})
    try:
        mgr.publish_ha_discovery()
        return JSONResponse({"ok": True, "message": "Discovery payloads published"})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})


@router.post("/mqtt/test", response_class=JSONResponse)
async def mqtt_test(
    request: Request,
    db: Session = Depends(get_db),
):
    import asyncio
    import mqtt_manager as _mqtt
    item = db.query(Item).first()
    if not item or not item.mqtt_host:
        return JSONResponse({"ok": False, "error": "No MQTT host configured — save settings first"})
    cfg = {
        "mqtt_host": item.mqtt_host,
        "mqtt_port": item.mqtt_port,
        "mqtt_username": item.mqtt_username,
        "mqtt_password": item.mqtt_password,
        "mqtt_topic_prefix": item.mqtt_topic_prefix or "iptv-manager",
        "mqtt_ha_discovery": False,
    }
    ok, msg = await asyncio.to_thread(_mqtt.test_mqtt_connection, cfg)
    return JSONResponse({"ok": ok, "message": msg})

