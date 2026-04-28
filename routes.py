from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates
import asyncio
import time
from sqlalchemy.orm import Session
from models import get_db, Item, AppConfig, get_app_config, set_app_config
from services import create_item, update_item, get_item_context, get_all_item_contexts, get_item_by_slug, generate_slug, get_generated_epg_count, write_count_to_cache
import config
import logging
import os
import re
from hdhomerun_routes import hdhomerun_emulator, get_active_streams, kill_stream, KILL_BLOCK_SECONDS
import urllib.parse
import json
from epg_manager import get_epg
from m3u_service import do_fetch_m3u, build_filter_config, apply_m3u_filter, refresh_filtered_playlist

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _do_refresh_filtered(item_id: int):
    """Background-safe filtered playlist rebuild for a single provider."""
    from models import SessionLocal, Item as _Item
    with SessionLocal() as db:
        item = db.query(_Item).filter(_Item.id == item_id).first()
        if item:
            refresh_filtered_playlist(item)


def _do_epg_refresh(force: bool = True):
    """Background-safe EPG rebuild: opens its own DB session to read hdhr_provider_id."""
    from models import SessionLocal
    with SessionLocal() as db:
        hdhr_id = get_app_config(db, "hdhr_provider_id")
        item_ids = [int(hdhr_id)] if hdhr_id else None
    get_epg(force_refresh=force, item_ids=item_ids)

def get_base_url(request: Request) -> str:
    return config.ADVERTISED_BASE_URL

@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db), error: str = None, success: str = None):
    base_url = get_base_url(request)
    items = get_all_item_contexts(db, base_url, config.M3U_DIR)
    # Keep single `item` for templates that still expect it (VPN quick actions etc.)
    item = items[0] if items else None
    ssdp_disabled_by_env = hdhomerun_emulator.is_env_disabled()
    start = time.perf_counter()
    template = templates.get_template("dashboard.html")
    rendered = template.render({
        "request": request,
        "item": item,
        "items": items,
        "error": error,
        "success": success,
        "base_url": base_url,
        "hdhr_running": hdhomerun_emulator.is_running(),
        "can_enable_ssdp": not ssdp_disabled_by_env,
        "friendly_name": config.HDHR_FRIENDLY_NAME,
        "active_page": "dashboard",
        "hdhr_filtered_count": sum(i["filtered_count"] for i in items),
        "hdhr_epg_count": get_generated_epg_count(config.M3U_DIR),
    })
    logger.debug(f"Template render duration: {time.perf_counter() - start:.3f}s")
    return HTMLResponse(content=rendered)


@router.get("/settings", response_class=RedirectResponse)
async def settings_page(db: Session = Depends(get_db)):
    first = db.query(Item).first()
    if first:
        return RedirectResponse(url=f"/providers/{first.id}", status_code=302)
    return RedirectResponse(url="/providers", status_code=302)


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
        result = update_item(db, existing.id, name=name, server_url=server_url, username=username, user_pass=user_pass, m3u_refresh_hours=m3u_refresh_hours, max_sessions=max_sessions)
    else:
        result = create_item(db, name, server_url, username, user_pass, None, None, None, m3u_refresh_hours=m3u_refresh_hours, max_sessions=max_sessions)
    if not result:
        return RedirectResponse(url="/providers?error=Failed+to+save+provider", status_code=303)
    return RedirectResponse(url=f"/providers/{result.id}?success=Provider+saved+successfully", status_code=303)

async def _test_provider_connection(item, db):
    import requests as _requests
    import asyncio
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


@router.get("/settings/test_connection", response_class=JSONResponse)
async def test_connection(request: Request, db: Session = Depends(get_db)):
    item = db.query(Item).first()
    if not item:
        return JSONResponse({"ok": False, "error": "No provider configured"})
    return await _test_provider_connection(item, db)


@router.get("/providers/{item_id}/test_connection", response_class=JSONResponse)
async def test_provider_connection(item_id: int, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        return JSONResponse({"ok": False, "error": "Provider not found"})
    return await _test_provider_connection(item, db)


@router.get("/api/active_streams", response_class=JSONResponse)
async def api_active_streams(db: Session = Depends(get_db)):
    import math
    streams = get_active_streams()
    all_items = db.query(Item).all()
    max_sessions = sum(int(it.max_sessions or 1) for it in all_items) if all_items else 1
    item_names = {it.id: it.name for it in all_items}
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
        provider_name = item_names.get(s.get("item_id"), "")
        out.append({
            "session_id": s.get("session_id", ""),
            "channel": channel_display,
            "client_ip": s.get("client_ip", "?"),
            "duration": duration,
            "mb_sent": round(mb, 1),
            "provider": provider_name,
        })
    return JSONResponse({"streams": out, "max_sessions": max_sessions})


@router.post("/api/streams/{session_id}/kill", response_class=JSONResponse)
async def kill_stream_api(session_id: str):
    ok = kill_stream(session_id)
    if not ok:
        return JSONResponse({"ok": False, "error": "Session not found"}, status_code=404)
    return JSONResponse({"ok": True, "block_seconds": KILL_BLOCK_SECONDS})




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
async def generate_m3u(background_tasks: BackgroundTasks, item_id: int = Form(...)):
    def _fetch():
        from models import SessionLocal
        with SessionLocal() as thread_db:
            return do_fetch_m3u(item_id, thread_db)
    try:
        ok, msg, _ = await asyncio.to_thread(_fetch)
        if not ok:
            return RedirectResponse(url=f"/providers/{item_id}?error={urllib.parse.quote(msg)}", status_code=303)
        background_tasks.add_task(_do_refresh_filtered, item_id)
        background_tasks.add_task(_do_epg_refresh)
        return RedirectResponse(url=f"/providers/{item_id}?success={urllib.parse.quote(msg)}", status_code=303)
    except Exception as e:
        logger.error(f"Failed to generate M3U for item {item_id}: {e}")
        return RedirectResponse(url=f"/providers/{item_id}?error={urllib.parse.quote(f'Failed to fetch M3U: {e}')}", status_code=303)

@router.post("/generate_filtered_m3u", response_class=RedirectResponse)
async def generate_filtered_m3u(background_tasks: BackgroundTasks, item_id: int = Form(...), db: Session = Depends(get_db)):
    try:
        item = db.query(Item).filter(Item.id == item_id).first()
        if not item:
            logger.warning(f"Item with id {item_id} not found for filtered M3U generation")
            return RedirectResponse(url="/providers?error=Item+not+found", status_code=303)

        m3u_path = os.path.join(config.M3U_DIR, f"xtream_playlist_{item_id}.m3u")
        if not os.path.exists(m3u_path):
            logger.warning(f"M3U file not found for item {item_id} at {m3u_path}")
            return RedirectResponse(url=f"/providers/{item_id}?error=M3U+file+not+found%2C+fetch+M3U+first", status_code=303)

        _item_snapshot = item

        def _do_provider_filter():
            with open(m3u_path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
            languages, includes_map, raw_includes, excludes, has_wildcard = build_filter_config(_item_snapshot)
            logger.debug(
                f"Filtering item {item_id}: languages={languages} includes={raw_includes} "
                f"excludes={excludes} wildcard={has_wildcard}"
            )
            filtered_content, num_records, input_record_count = apply_m3u_filter(
                lines, languages, includes_map, excludes, has_wildcard
            )
            if num_records == 0:
                return None, 0, input_record_count
            os.makedirs(config.M3U_DIR, exist_ok=True)
            filtered_path = os.path.join(config.M3U_DIR, f"filtered_playlist_{item_id}.m3u")
            tmp_path = filtered_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(filtered_content)
            os.replace(tmp_path, filtered_path)
            write_count_to_cache(config.M3U_DIR, str(item_id), "filtered_count", num_records, filtered_path)
            total_lines = len(filtered_content.splitlines())
            logger.info(
                f"Filtered M3U for item {item_id}: input={input_record_count} kept={num_records} lines={total_lines}"
            )
            return filtered_content, num_records, input_record_count

        filtered_content, num_records, input_record_count = await asyncio.to_thread(_do_provider_filter)

        if num_records == 0:
            logger.warning(f"No records matched filter for item {item_id}")
            return RedirectResponse(url=f"/providers/{item_id}?error=No+records+matched+the+filter+criteria.", status_code=303)

        success_msg = urllib.parse.quote(
            f"Filtered {num_records} of {input_record_count} records"
        )
        background_tasks.add_task(_do_epg_refresh)
        logger.debug("EPG rebuild queued in background after filtered M3U save")
        return RedirectResponse(url=f"/providers/{item_id}?success={success_msg}", status_code=303)

    except Exception as e:
        logger.error(f"Failed to generate filtered M3U for item {item_id}: {str(e)}")
        return RedirectResponse(url=f"/providers/{item_id}?error=Failed+to+save+filtered+M3U+file%3A+{urllib.parse.quote(str(e))}", status_code=303)

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

    # Iterate line-by-line to avoid loading the full 58 MB file into RAM at once
    prev_extinf: str | None = None
    first_line_checked = False
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not first_line_checked:
                first_line_checked = True
                if line.strip() == "#EXTM3U":
                    continue
            if line.startswith("#EXTINF"):
                prev_extinf = line
            elif prev_extinf is not None:
                extinf = prev_extinf
                url = line.strip()
                prev_extinf = None
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
            else:
                prev_extinf = None

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
    items = get_all_item_contexts(db, base_url, config.M3U_DIR)
    hdhr_provider_id = get_app_config(db, "hdhr_provider_id")
    hdhr_stream_provider_id = get_app_config(db, "hdhr_stream_provider_id")
    # Resolve the selected provider for the channel filter form
    if hdhr_provider_id:
        selected_item = get_item_context(db, base_url, config.M3U_DIR, db.query(Item).filter(Item.id == int(hdhr_provider_id)).first())
    else:
        selected_item = items[0] if items else None
    ssdp_disabled_by_env = hdhomerun_emulator.is_env_disabled()
    start = time.perf_counter()
    template = templates.get_template("hdhomerun.html")
    rendered = template.render({
        "request": request,
        "item": selected_item,
        "items": items,
        "hdhr_provider_id": hdhr_provider_id,
        "hdhr_stream_provider_id": hdhr_stream_provider_id,
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


@router.post("/hdhomerun/select_provider", response_class=RedirectResponse)
async def hdhomerun_select_provider(
    provider_id: str = Form(""),
    stream_provider_id: str = Form(""),
    db: Session = Depends(get_db),
):
    set_app_config(db, "hdhr_provider_id", provider_id or None)
    set_app_config(db, "hdhr_stream_provider_id", stream_provider_id or None)
    return RedirectResponse(url="/hdhomerun?success=Provider+selection+saved", status_code=303)


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
        result = update_item(db, item_id, includes=new_includes or None, m3u_refresh_hours=m3u_refresh_hours)
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
            _item_snapshot = item

            def _do_hdhr_filter():
                with open(m3u_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
                languages, includes_map, raw_includes, excludes, has_wildcard = build_filter_config(_item_snapshot)
                if not includes_map and not languages and not excludes:
                    return None, 0, 0
                filtered_content, num_records, input_record_count = apply_m3u_filter(
                    lines, languages, includes_map, excludes, has_wildcard
                )
                if num_records == 0:
                    return filtered_content, 0, input_record_count
                os.makedirs(config.M3U_DIR, exist_ok=True)
                filtered_path = os.path.join(config.M3U_DIR, f"filtered_playlist_{item_id}.m3u")
                tmp_path = filtered_path + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(filtered_content)
                os.replace(tmp_path, filtered_path)
                write_count_to_cache(config.M3U_DIR, str(item_id), "filtered_count", num_records, filtered_path)
                return filtered_content, num_records, input_record_count

            filtered_content, num_records, input_record_count = await asyncio.to_thread(_do_hdhr_filter)

            if filtered_content is None:
                return RedirectResponse(
                    url="/hdhomerun?error=Channel list is empty — add channels in the format: 100|ESPN",
                    status_code=303,
                )
            if num_records == 0:
                return RedirectResponse(
                    url="/hdhomerun?error=Filters saved but no channels matched — check your filter list",
                    status_code=303,
                )

            background_tasks.add_task(_do_epg_refresh)
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


@router.get("/xtream", response_class=RedirectResponse)
async def xtream_page(db: Session = Depends(get_db)):
    first = db.query(Item).first()
    if first:
        return RedirectResponse(url=f"/providers/{first.id}", status_code=302)
    return RedirectResponse(url="/providers", status_code=302)


@router.post("/xtream", response_class=RedirectResponse)
async def handle_xtream_form(
    request: Request,
    save_xtream_filters: str = Form(None),
    item_id: int = Form(None),
    new_xtream_includes: str = Form(None),
    db: Session = Depends(get_db),
):
    if save_xtream_filters and item_id:
        if new_xtream_includes and '\n' in new_xtream_includes:
            new_xtream_includes = ','.join([x.strip() for x in new_xtream_includes.split('\n') if x.strip()])
        result = update_item(db, item_id, xtream_includes=new_xtream_includes)
        if not result:
            return RedirectResponse(url=f"/providers/{item_id}?error=Failed+to+save+Xtream+filters", status_code=303)
        return RedirectResponse(url=f"/providers/{item_id}?success=Xtream+filters+saved", status_code=303)
    first = db.query(Item).first()
    return RedirectResponse(url=f"/providers/{first.id}" if first else "/providers", status_code=303)


# ---------------------------------------------------------------------------
# VPN endpoints
# ---------------------------------------------------------------------------

@router.post("/vpn/settings", response_class=RedirectResponse)
async def save_vpn_settings(
    vpn_config: str = Form(""),
    vpn_username: str = Form(""),
    vpn_password: str = Form(""),
    item_id: int = Form(0),
    db: Session = Depends(get_db),
):
    item = db.query(Item).first()
    if not item:
        return RedirectResponse(url="/providers?error=No+provider+configured", status_code=303)
    if vpn_config.strip():
        item.vpn_config = vpn_config.strip()
    if vpn_username.strip():
        item.vpn_username = vpn_username.strip()
    if vpn_password.strip():
        item.vpn_password = vpn_password.strip()
    db.commit()
    redirect_id = item_id if item_id else item.id
    return RedirectResponse(url=f"/providers/{redirect_id}?success=VPN+settings+saved", status_code=303)


@router.post("/vpn/enable", response_class=RedirectResponse)
async def vpn_enable(request: Request, db: Session = Depends(get_db)):
    import vpn_manager
    import asyncio
    form = await request.form()
    next_url = form.get("next") or "/"
    item = db.query(Item).first()
    if not item:
        return RedirectResponse(url="/providers?error=No+provider+configured", status_code=303)
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
    form = await request.form()
    next_url = form.get("next") or "/"
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
    import vpn_manager
    base_url = get_base_url(request)
    item = get_item_context(db, base_url, config.M3U_DIR)
    all_items = db.query(Item).all()
    provider_count = len(all_items)
    mqtt_device_count = sum(1 for it in all_items if it.mqtt_topic_prefix and it.mqtt_topic_prefix.strip())
    vpn_status = await asyncio.to_thread(vpn_manager.get_vpn_status)
    start = time.perf_counter()
    template = templates.get_template("tools.html")
    rendered = template.render({
        "request": request,
        "item": item,
        "friendly_name": config.HDHR_FRIENDLY_NAME,
        "active_page": "tools",
        "base_url": base_url,
        "hdhr_running": hdhomerun_emulator.is_running(),
        "provider_count": provider_count,
        "mqtt_device_count": mqtt_device_count,
        "timezone": os.getenv("TZ", "UTC"),
        "container_name": os.getenv("CONTAINER_NAME", ""),
        "vpn_running": vpn_status.get("running", False),
        "vpn_interface": vpn_status.get("interface", ""),
        "error": error,
        "success": success,
    })
    logger.debug(f"Template render duration: {time.perf_counter() - start:.3f}s")
    return HTMLResponse(content=rendered)


async def _save_mqtt_to_item(item, db, mqtt_enabled, mqtt_host, mqtt_port, mqtt_username, mqtt_password, redirect_base: str):
    import mqtt_manager as _mqtt
    enabled = mqtt_enabled.lower() in ("1", "true", "on", "yes")
    item.mqtt_enabled = enabled
    item.mqtt_host = mqtt_host.strip() or None
    item.mqtt_port = mqtt_port
    item.mqtt_username = mqtt_username.strip() or None
    if mqtt_password:
        item.mqtt_password = mqtt_password
    db.commit()
    cfg = {
        "mqtt_host": item.mqtt_host,
        "mqtt_port": item.mqtt_port,
        "mqtt_username": item.mqtt_username,
        "mqtt_password": item.mqtt_password,
        "mqtt_topic_prefix": item.mqtt_topic_prefix,
        "mqtt_device_name": item.mqtt_device_name,
    }
    _sync_provider_mqtt_configs(db)
    if enabled and item.mqtt_host:
        ok, msg = _mqtt.start_mqtt(cfg)
        if not ok:
            return RedirectResponse(url=f"{redirect_base}?error={urllib.parse.quote('MQTT saved but connect failed: ' + msg)}", status_code=303)
    else:
        _mqtt.stop_mqtt()
    return RedirectResponse(url=f"{redirect_base}?success=MQTT+settings+saved", status_code=303)


@router.post("/mqtt/settings", response_class=RedirectResponse)
async def save_mqtt_settings(
    request: Request,
    db: Session = Depends(get_db),
    mqtt_enabled: str = Form(""),
    mqtt_host: str = Form(""),
    mqtt_port: int = Form(1883),
    mqtt_username: str = Form(""),
    mqtt_password: str = Form(""),
):
    item = db.query(Item).first()
    if not item:
        return RedirectResponse(url="/tools?error=No+provider+configured", status_code=303)
    return await _save_mqtt_to_item(item, db, mqtt_enabled, mqtt_host, mqtt_port, mqtt_username, mqtt_password, "/tools")


def _sync_provider_mqtt_configs(db) -> None:
    """Push all providers' MQTT prefix configs to the running manager."""
    import mqtt_manager as _mqtt
    all_items = db.query(Item).all()
    configs = [
        {"item_id": it.id, "prefix": it.mqtt_topic_prefix, "device_name": it.mqtt_device_name or ""}
        for it in all_items
        if it.mqtt_topic_prefix and it.mqtt_topic_prefix.strip()
    ]
    _mqtt.set_provider_configs(configs)


def _discovery_transition(old_prefix: str | None, new_prefix: str | None, new_device_name: str):
    """Background task: retract old HA discovery, wait, then republish and push state."""
    import time
    import mqtt_manager as _mqtt
    if old_prefix and old_prefix != new_prefix:
        _mqtt.retract_ha_discovery(old_prefix)
        time.sleep(2)
    if new_prefix:
        _mqtt.publish_ha_discovery_for(new_prefix, new_device_name or new_prefix)
        _mqtt.force_publish_state()


@router.post("/providers/{item_id}/mqtt", response_class=RedirectResponse)
async def save_provider_mqtt(
    item_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    mqtt_topic_prefix: str = Form(""),
    mqtt_device_name: str = Form(""),
):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        return RedirectResponse(url="/providers?error=Provider+not+found", status_code=303)

    old_prefix = item.mqtt_topic_prefix
    old_device_name = item.mqtt_device_name
    new_prefix = mqtt_topic_prefix.strip() or None
    new_device_name = mqtt_device_name.strip() or None

    item.mqtt_topic_prefix = new_prefix
    item.mqtt_device_name = new_device_name
    db.commit()

    import mqtt_manager as _mqtt
    _sync_provider_mqtt_configs(db)

    prefix_changed = old_prefix != new_prefix
    name_changed = old_device_name != new_device_name
    if (prefix_changed or name_changed) and _mqtt.get_mqtt_status().get("connected"):
        background_tasks.add_task(_discovery_transition, old_prefix, new_prefix, new_device_name or "")

    return RedirectResponse(url=f"/providers/{item_id}?success=MQTT+identity+saved", status_code=303)


@router.get("/mqtt/status", response_class=JSONResponse)
async def mqtt_status():
    import mqtt_manager as _mqtt
    return JSONResponse(_mqtt.get_mqtt_status())


@router.post("/mqtt/ha_discovery", response_class=JSONResponse)
async def mqtt_ha_discovery(db: Session = Depends(get_db)):
    import mqtt_manager as _mqtt
    if not _mqtt.get_mqtt_status().get("connected"):
        return JSONResponse({"ok": False, "error": "MQTT not connected — connect first"})
    items = db.query(Item).filter(Item.mqtt_topic_prefix != None, Item.mqtt_topic_prefix != "").all()
    if not items:
        return JSONResponse({"ok": False, "error": "No providers have a topic prefix configured"})
    count = sum(
        1 for it in items
        if _mqtt.publish_ha_discovery_for(it.mqtt_topic_prefix, it.mqtt_device_name or "")
    )
    return JSONResponse({"ok": True, "message": f"Published discovery for {count} device(s)"})


@router.post("/mqtt/ha_discovery/remove", response_class=JSONResponse)
async def mqtt_ha_discovery_remove(db: Session = Depends(get_db)):
    import mqtt_manager as _mqtt
    if not _mqtt.get_mqtt_status().get("connected"):
        return JSONResponse({"ok": False, "error": "MQTT not connected — connect first"})
    items = db.query(Item).filter(Item.mqtt_topic_prefix != None, Item.mqtt_topic_prefix != "").all()
    if not items:
        return JSONResponse({"ok": False, "error": "No providers have a topic prefix configured"})
    count = sum(1 for it in items if _mqtt.retract_ha_discovery(it.mqtt_topic_prefix))
    return JSONResponse({"ok": True, "message": f"Removed discovery for {count} device(s)"})


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


# ---------------------------------------------------------------------------
# Providers CRUD
# ---------------------------------------------------------------------------

@router.get("/providers", response_class=HTMLResponse)
async def providers_list(request: Request, db: Session = Depends(get_db), error: str = None, success: str = None):
    base_url = get_base_url(request)
    items = get_all_item_contexts(db, base_url, config.M3U_DIR)
    template = templates.get_template("providers.html")
    rendered = template.render({
        "request": request,
        "items": items,
        "base_url": base_url,
        "friendly_name": config.HDHR_FRIENDLY_NAME,
        "active_page": "providers",
        "error": error,
        "success": success,
    })
    return HTMLResponse(content=rendered)


@router.get("/providers/new", response_class=HTMLResponse)
async def provider_new_form(request: Request, db: Session = Depends(get_db), error: str = None):
    template = templates.get_template("provider_edit.html")
    rendered = template.render({
        "request": request,
        "item": None,
        "friendly_name": config.HDHR_FRIENDLY_NAME,
        "active_page": "providers",
        "is_new": True,
        "error": error,
    })
    return HTMLResponse(content=rendered)


@router.post("/providers/new", response_class=RedirectResponse)
async def provider_create(
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    server_url: str = Form(...),
    username: str = Form(...),
    user_pass: str = Form(...),
    proxy_username: str = Form("iptv"),
    proxy_password: str = Form("iptv"),
    m3u_refresh_hours: int = Form(0),
    max_sessions: int = Form(1),
    db: Session = Depends(get_db),
):
    slug = slug.strip() or None
    if slug:
        existing = db.query(Item).filter(Item.slug == slug).first()
        if existing:
            return RedirectResponse(url=f"/providers/new?error=Slug+'{slug}'+already+in+use", status_code=303)
    result = create_item(
        db, name, server_url, username, user_pass,
        languages=None, includes=None, excludes=None,
        m3u_refresh_hours=m3u_refresh_hours, max_sessions=max_sessions,
        slug=slug,
        proxy_username=proxy_username or "iptv",
        proxy_password=proxy_password or "iptv",
    )
    if not result:
        return RedirectResponse(url="/providers/new?error=Failed+to+create+provider", status_code=303)
    return RedirectResponse(url=f"/providers/{result.id}?success=Provider+created+successfully", status_code=303)


@router.get("/providers/{item_id}", response_class=HTMLResponse)
async def provider_edit_form(
    item_id: int,
    request: Request,
    db: Session = Depends(get_db),
    error: str = None,
    success: str = None,
):
    base_url = get_base_url(request)
    db_item = db.query(Item).filter(Item.id == item_id).first()
    if not db_item:
        return RedirectResponse(url="/providers?error=Provider+not+found", status_code=302)
    item = get_item_context(db, base_url, config.M3U_DIR, db_item)
    template = templates.get_template("provider_edit.html")
    rendered = template.render({
        "request": request,
        "item": item,
        "friendly_name": config.HDHR_FRIENDLY_NAME,
        "active_page": "providers",
        "is_new": False,
        "allow_full_m3u_download": config.ALLOW_FULL_M3U_DOWNLOAD,
        "base_url": base_url,
        "error": error,
        "success": success,
    })
    return HTMLResponse(content=rendered)


@router.post("/providers/{item_id}", response_class=RedirectResponse)
async def provider_save(
    item_id: int,
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    server_url: str = Form(...),
    username: str = Form(...),
    user_pass: str = Form(...),
    proxy_username: str = Form("iptv"),
    proxy_password: str = Form("iptv"),
    m3u_refresh_hours: int = Form(0),
    max_sessions: int = Form(1),
    db: Session = Depends(get_db),
):
    slug = slug.strip() or None
    if slug:
        conflict = db.query(Item).filter(Item.slug == slug, Item.id != item_id).first()
        if conflict:
            return RedirectResponse(url=f"/providers/{item_id}?error=Slug+'{slug}'+already+in+use+by+another+provider", status_code=303)
    result = update_item(
        db, item_id,
        name=name, slug=slug, server_url=server_url,
        username=username, user_pass=user_pass,
        proxy_username=proxy_username or "iptv",
        proxy_password=proxy_password or "iptv",
        m3u_refresh_hours=m3u_refresh_hours,
        max_sessions=max_sessions,
    )
    if not result:
        return RedirectResponse(url=f"/providers/{item_id}?error=Failed+to+save+provider", status_code=303)
    return RedirectResponse(url=f"/providers/{item_id}?success=Provider+saved+successfully", status_code=303)


@router.post("/providers/{item_id}/filters", response_class=RedirectResponse)
async def provider_save_filters(
    item_id: int,
    background_tasks: BackgroundTasks,
    new_includes: str = Form(""),
    excludes: str = Form(""),
    languages: str = Form(""),
    new_xtream_includes: str = Form(""),
    db: Session = Depends(get_db),
):
    if new_includes and '\n' in new_includes:
        new_includes = ','.join([x.strip() for x in new_includes.split('\n') if x.strip()])
    if new_xtream_includes and '\n' in new_xtream_includes:
        new_xtream_includes = ','.join([x.strip() for x in new_xtream_includes.split('\n') if x.strip()])
    result = update_item(
        db, item_id,
        includes=new_includes or None,
        excludes=excludes or None,
        languages=languages or None,
        xtream_includes=new_xtream_includes or None,
    )
    if not result:
        return RedirectResponse(url=f"/providers/{item_id}?error=Failed+to+save+filters", status_code=303)

    m3u_path = os.path.join(config.M3U_DIR, f"xtream_playlist_{item_id}.m3u")
    if not os.path.exists(m3u_path):
        return RedirectResponse(
            url=f"/providers/{item_id}?success=Filters+saved+%E2%80%94+fetch+M3U+first+to+apply",
            status_code=303,
        )

    try:
        item = db.query(Item).filter(Item.id == item_id).first()
        _item_snapshot = item

        def _do_save_filters():
            with open(m3u_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            languages_cfg, includes_map, raw_includes, excludes_cfg, has_wildcard = build_filter_config(_item_snapshot)
            if not includes_map and not languages_cfg and not excludes_cfg:
                return None, 0, 0
            filtered_content, num_records, input_count = apply_m3u_filter(
                lines, languages_cfg, includes_map, excludes_cfg, has_wildcard
            )
            if num_records == 0:
                return filtered_content, 0, input_count
            filtered_path = os.path.join(config.M3U_DIR, f"filtered_playlist_{item_id}.m3u")
            tmp_path = filtered_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(filtered_content)
            os.replace(tmp_path, filtered_path)
            write_count_to_cache(config.M3U_DIR, str(item_id), "filtered_count", num_records, filtered_path)
            return filtered_content, num_records, input_count

        filtered_content, num_records, input_count = await asyncio.to_thread(_do_save_filters)

        if filtered_content is None:
            return RedirectResponse(
                url=f"/providers/{item_id}?error=Channel+list+is+empty+%E2%80%94+add+channels+in+the+format%3A+100%7CESPN",
                status_code=303,
            )
        if num_records == 0:
            return RedirectResponse(
                url=f"/providers/{item_id}?error=Filters+saved+but+no+channels+matched",
                status_code=303,
            )
        background_tasks.add_task(_do_epg_refresh)
        msg = urllib.parse.quote(f"Filters saved — {num_records} of {input_count} channels matched")
        return RedirectResponse(url=f"/providers/{item_id}?success={msg}", status_code=303)
    except Exception as e:
        logger.error(f"Failed to apply filters for item {item_id}: {e}")
        return RedirectResponse(
            url=f"/providers/{item_id}?error=Filters+saved+but+filtering+failed", status_code=303
        )


@router.post("/providers/{item_id}/delete", response_class=RedirectResponse)
async def provider_delete(item_id: int, db: Session = Depends(get_db)):
    total = db.query(Item).count()
    if total <= 1:
        return RedirectResponse(url=f"/providers/{item_id}?error=Cannot+delete+the+only+provider", status_code=303)
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        return RedirectResponse(url="/providers?error=Provider+not+found", status_code=303)

    # Retract HA discovery before losing the provider's prefix
    if item.mqtt_topic_prefix:
        import mqtt_manager as _mqtt
        if _mqtt.get_mqtt_status().get("connected"):
            _mqtt.retract_ha_discovery(item.mqtt_topic_prefix)

    # Reset HDHomeRun selection if this provider was selected
    hdhr_sel = get_app_config(db, "hdhr_provider_id")
    if hdhr_sel and int(hdhr_sel) == item_id:
        set_app_config(db, "hdhr_provider_id", None)
    db.delete(item)
    db.commit()
    _sync_provider_mqtt_configs(db)
    return RedirectResponse(url="/providers?success=Provider+deleted", status_code=303)

