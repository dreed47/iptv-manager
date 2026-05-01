from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates
import asyncio
import time
import threading
from sqlalchemy.orm import Session
from models import get_db, Item, AppConfig, get_app_config, set_app_config, SessionLocal, get_epg_channel_map, save_epg_channel_map, EpgChannelMap
from services import create_item, update_item, get_item_context, get_all_item_contexts, get_item_by_slug, generate_slug, get_generated_epg_count, write_count_to_cache
import config
import logging
import os
import re
from hdhomerun_routes import hdhomerun_emulator, get_active_streams, kill_stream, KILL_BLOCK_SECONDS
import urllib.parse
import json
from epg_manager import get_epg, run_epg_build, EPG_CACHE_PATH
from m3u_service import do_fetch_m3u, build_filter_config, apply_m3u_filter, refresh_filtered_playlist

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")


_FILTER_REFRESH_LOCK = threading.Lock()
_FILTER_REFRESH_INFLIGHT: set[int] = set()

_EPG_REFRESH_LOCK = threading.Lock()
_EPG_REFRESH_INFLIGHT = False
_EPG_LAST_START = 0.0
_EPG_COOLDOWN_S = float(os.getenv("EPG_REFRESH_COOLDOWN_S", "60"))


def _do_refresh_filtered(item_id: int):
    """Rebuild filtered playlist for a provider (safe to call from a worker thread)."""
    from models import SessionLocal, Item as _Item
    with SessionLocal() as db:
        item = db.query(_Item).filter(_Item.id == item_id).first()
        if item:
            refresh_filtered_playlist(item)


def queue_refresh_filtered(item_id: int) -> bool:
    """Queue filtered playlist rebuild out-of-process and return immediately."""
    with _FILTER_REFRESH_LOCK:
        if item_id in _FILTER_REFRESH_INFLIGHT:
            return False
        _FILTER_REFRESH_INFLIGHT.add(item_id)

    def _run():
        try:
            import multiprocessing as mp
            from m3u_service import run_filtered_refresh
            p = mp.Process(target=run_filtered_refresh, args=(item_id,), daemon=True)
            p.start()
            p.join()
        except Exception as exc:
            logger.warning(f"Failed to start filtered refresh for item {item_id}: {exc}")
        finally:
            with _FILTER_REFRESH_LOCK:
                _FILTER_REFRESH_INFLIGHT.discard(item_id)

    threading.Thread(target=_run, name=f"filtered-refresh-{item_id}", daemon=True).start()
    return True


def _resolve_epg_params() -> tuple[list[int] | None, dict[str, str], int | None]:
    """Read EPG build params from DB: item_ids, channel_map, time_offset_hours."""
    from models import SessionLocal
    with SessionLocal() as db:
        hdhr_id = get_app_config(db, "hdhr_provider_id")
        item_ids = [int(hdhr_id)] if hdhr_id else None
        channel_map = get_epg_channel_map(db)
        raw_offset = get_app_config(db, "epg_time_offset_hours")
        time_offset = int(raw_offset) if raw_offset is not None else None
    return item_ids, channel_map, time_offset


def queue_epg_refresh(force: bool = True) -> bool:
    """Run EPG build out-of-process to avoid blocking the asyncio event loop."""
    global _EPG_REFRESH_INFLIGHT, _EPG_LAST_START

    now = time.monotonic()
    with _EPG_REFRESH_LOCK:
        if _EPG_REFRESH_INFLIGHT:
            return False
        if _EPG_COOLDOWN_S > 0 and (now - _EPG_LAST_START) < _EPG_COOLDOWN_S:
            return False
        _EPG_REFRESH_INFLIGHT = True
        _EPG_LAST_START = now

    def _run():
        global _EPG_REFRESH_INFLIGHT
        try:
            item_ids, channel_map, time_offset = _resolve_epg_params()
            import multiprocessing as mp
            p = mp.Process(target=run_epg_build,
                           args=(force, item_ids, channel_map, time_offset), daemon=True)
            p.start()
            p.join()
        except Exception as exc:
            logger.warning(f"EPG refresh failed to start: {exc}")
        finally:
            with _EPG_REFRESH_LOCK:
                _EPG_REFRESH_INFLIGHT = False

    threading.Thread(target=_run, name="epg-refresh", daemon=True).start()
    return True


def _do_epg_refresh(force: bool = True):
    """Compatibility wrapper for existing BackgroundTasks.add_task calls."""
    queue_epg_refresh(force=force)

def get_base_url(request: Request) -> str:
    return config.ADVERTISED_BASE_URL

@router.get("/", response_class=HTMLResponse)
async def index(request: Request, error: str = None, success: str = None):
    base_url = get_base_url(request)

    def _load_dashboard_context():
        # Run DB + filesystem work off the event loop. Create a fresh DB session
        # in this thread to avoid cross-thread SQLAlchemy usage.
        with SessionLocal() as db:
            items = get_all_item_contexts(db, base_url, config.M3U_DIR)
        epg_count = get_generated_epg_count(config.M3U_DIR)
        from m3u_service import HDHR_PLAYLIST_FILENAME
        from services import _cached_count, _count_extinf, _load_count_cache
        hdhr_playlist_path = os.path.join(config.M3U_DIR, HDHR_PLAYLIST_FILENAME)
        hdhr_has_filtered = os.path.exists(hdhr_playlist_path)
        cache_path = os.path.join(config.M3U_DIR, "counts_hdhr.json")
        cache = _load_count_cache(cache_path)
        hdhr_filtered_count, _ = _cached_count(hdhr_playlist_path, _count_extinf, cache, "filtered_count", "filtered_mtime")
        return items, epg_count, hdhr_filtered_count, hdhr_has_filtered

    items, epg_count, hdhr_filtered_count, hdhr_has_filtered = await asyncio.to_thread(_load_dashboard_context)

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
        "hdhr_filtered_count": hdhr_filtered_count,
        "hdhr_has_filtered": hdhr_has_filtered,
        "hdhr_epg_count": epg_count,
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
    streams = get_active_streams()
    all_items = db.query(Item).all()
    max_sessions = sum(int(it.max_sessions or 1) for it in all_items) if all_items else 1
    item_names = {it.id: it.name for it in all_items}

    # Group by (client_ip, channel_name) — multi-connection clients (e.g. Apple TV) show as one entry
    groups: dict[tuple, list] = {}
    for s in streams:
        key = (s.get("client_ip", "?"), s.get("channel_name", s.get("channel", "?")))
        groups.setdefault(key, []).append(s)

    now = time.time()
    out = []
    for group in groups.values():
        rep = max(group, key=lambda s: s.get("bytes_sent", 0))
        elapsed = int(now - rep.get("started_at", now))
        hours, rem = divmod(elapsed, 3600)
        mins, secs = divmod(rem, 60)
        duration = f"{hours}h {mins}m" if hours else f"{mins}m {secs}s"
        mb = rep.get("bytes_sent", 0) / (1024 * 1024)
        channel_num = rep.get("channel", "?")
        channel_name = rep.get("channel_name", "")
        channel_display = f"{channel_num} — {channel_name}" if channel_name and channel_name != channel_num else channel_num
        provider_name = item_names.get(rep.get("item_id"), "")
        entry = {
            "session_id": rep.get("session_id", ""),
            "channel": channel_display,
            "client_ip": rep.get("client_ip", "?"),
            "duration": duration,
            "mb_sent": round(mb, 1),
            "provider": provider_name,
        }
        if len(group) > 1:
            entry["connections"] = len(group)
        out.append(entry)
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


@router.get("/api/jobs", response_class=JSONResponse)
async def api_jobs():
    """Expose in-flight background jobs for the UI."""
    now = time.monotonic()
    with _FILTER_REFRESH_LOCK:
        filtered = sorted(_FILTER_REFRESH_INFLIGHT)
    with _EPG_REFRESH_LOCK:
        epg_inflight = bool(_EPG_REFRESH_INFLIGHT)
        epg_last_start = float(_EPG_LAST_START)

    cooldown_remaining = 0.0
    if _EPG_COOLDOWN_S > 0 and epg_last_start:
        cooldown_remaining = max(0.0, _EPG_COOLDOWN_S - (now - epg_last_start))

    return JSONResponse({
        "filtered_refresh": {"inflight": filtered},
        "epg": {
            "inflight": epg_inflight,
            "cooldown_remaining_s": round(cooldown_remaining, 1),
        },
    })


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
        background_tasks.add_task(queue_refresh_filtered, item_id)
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

        def _do_provider_filter():
            # Don't pass SQLAlchemy ORM objects across threads; reload inside the worker.
            from models import SessionLocal, Item as _Item
            with SessionLocal() as thread_db:
                it = thread_db.query(_Item).filter(_Item.id == item_id).first()
                if not it:
                    return None, 0, 0
                languages, includes_strict, includes_raw, raw_includes, excludes, has_wildcard = build_filter_config(it)

            with open(m3u_path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()

            logger.debug(
                f"Filtering item {item_id}: languages={languages} includes={raw_includes} "
                f"excludes={excludes} wildcard={has_wildcard}"
            )
            filtered_content, num_records, input_record_count = apply_m3u_filter(
                lines, languages, includes_strict, includes_raw, excludes, has_wildcard
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

        # Run filtering in the background to avoid long POST hangs.
        queue_refresh_filtered(item_id)
        background_tasks.add_task(_do_epg_refresh)
        return RedirectResponse(
            url=f"/providers/{item_id}?success=Filtered+playlist+refresh+queued",
            status_code=303,
        )

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
    search: str = "",
    group: str = "",
    prefix: str = "",
    page: int = 1,
    per_page: int = 100,
):
    per_page = max(10, min(per_page, 500))
    page = max(1, page)

    def _load():
        # Run parsing off the event loop — this file can be ~50–60MB with 200K+ entries.
        from models import SessionLocal
        with SessionLocal() as db:
            item = db.query(Item).filter(Item.id == item_id).first()
            if not item:
                return ("not_found", None)

        file_path = os.path.join(config.M3U_DIR, f"xtream_playlist_{item_id}.m3u")
        if not os.path.exists(file_path):
            return ("missing_m3u", None)

        s = (search or "").lower().strip()
        group_f = (group or "").strip()
        prefix_f = (prefix or "").strip()

        groups: set[str] = set()
        prefixes: dict[str, int] = {}
        page_rows: list[dict] = []

        matched = 0
        start = (page - 1) * per_page
        end = start + per_page

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
                    continue

                if prev_extinf is None:
                    continue

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

                # Apply filters (streaming)
                if s and (s not in display_name.lower() and s not in tvg_name.lower()):
                    continue
                if group_f and group_title != group_f:
                    continue
                if prefix_f and ch_prefix != prefix_f:
                    continue

                if matched >= start and matched < end:
                    page_rows.append({
                        "name": display_name,
                        "tvg_name": tvg_name,
                        "group": group_title,
                        "prefix": ch_prefix,
                        "url": url,
                    })
                matched += 1

        # Only include prefixes that appear on at least 5 channels (avoids one-off channel name colons)
        MIN_PREFIX_COUNT = 5
        valid_prefixes = {p for p, count in prefixes.items() if count >= MIN_PREFIX_COUNT}

        # Clear prefix on returned page rows whose detected prefix isn't a real provider
        for ch in page_rows:
            if ch["prefix"] and ch["prefix"] not in valid_prefixes:
                ch["prefix"] = ""

        total = matched
        total_pages = max(1, (total + per_page - 1) // per_page)

        return (
            "ok",
            {
                "channels": page_rows,
                "total": total,
                "page": min(page, total_pages),
                "per_page": per_page,
                "total_pages": total_pages,
                "groups": sorted(groups),
                "prefixes": sorted(valid_prefixes),
            },
        )

    status, payload = await asyncio.to_thread(_load)
    if status == "not_found":
        raise HTTPException(status_code=404, detail="Item not found")
    if status == "missing_m3u":
        raise HTTPException(status_code=404, detail="Full M3U not found")
    return payload


@router.get("/hdhomerun", response_class=HTMLResponse)
async def hdhomerun_page(request: Request, error: str = None, success: str = None):
    base_url = get_base_url(request)

    def _load():
        with SessionLocal() as db:
            items = get_all_item_contexts(db, base_url, config.M3U_DIR)
            hdhr_provider_id = get_app_config(db, "hdhr_provider_id")
            hdhr_stream_provider_id = get_app_config(db, "hdhr_stream_provider_id")
            hdhr_includes = get_app_config(db, "hdhr_includes") or ""
            raw_offset = get_app_config(db, "epg_time_offset_hours")
            epg_time_offset_hours = int(raw_offset) if raw_offset is not None else config.EPG_TIME_OFFSET_HOURS

        selected_item = None
        if hdhr_provider_id:
            try:
                sel_id = int(hdhr_provider_id)
                selected_item = next((i for i in items if i.get("id") == sel_id), None)
            except (TypeError, ValueError):
                selected_item = None
        if selected_item is None:
            selected_item = items[0] if items else None

        from m3u_service import HDHR_PLAYLIST_FILENAME
        hdhr_has_filtered = os.path.exists(os.path.join(config.M3U_DIR, HDHR_PLAYLIST_FILENAME))

        return items, hdhr_provider_id, hdhr_stream_provider_id, selected_item, hdhr_includes, hdhr_has_filtered, epg_time_offset_hours

    items, hdhr_provider_id, hdhr_stream_provider_id, selected_item, hdhr_includes, hdhr_has_filtered, epg_time_offset_hours = await asyncio.to_thread(_load)

    ssdp_disabled_by_env = hdhomerun_emulator.is_env_disabled()
    start = time.perf_counter()
    template = templates.get_template("hdhomerun.html")
    rendered = template.render({
        "request": request,
        "item": selected_item,
        "items": items,
        "hdhr_provider_id": hdhr_provider_id,
        "hdhr_stream_provider_id": hdhr_stream_provider_id,
        "hdhr_includes": hdhr_includes,
        "hdhr_has_filtered": hdhr_has_filtered,
        "friendly_name": config.HDHR_FRIENDLY_NAME,
        "active_page": "hdhomerun",
        "hdhr_running": hdhomerun_emulator.is_running(),
        "can_enable_ssdp": not ssdp_disabled_by_env,
        "allow_full_m3u_download": config.ALLOW_FULL_M3U_DOWNLOAD,
        "epg_time_offset_hours": epg_time_offset_hours,
        "error": error,
        "success": success,
    })
    logger.debug(f"Template render duration: {time.perf_counter() - start:.3f}s")
    return HTMLResponse(content=rendered)


@router.post("/hdhomerun/select_provider", response_class=RedirectResponse)
async def hdhomerun_select_provider(
    background_tasks: BackgroundTasks,
    provider_id: str = Form(""),
    stream_provider_id: str = Form(""),
    db: Session = Depends(get_db),
):
    set_app_config(db, "hdhr_provider_id", provider_id or None)
    set_app_config(db, "hdhr_stream_provider_id", stream_provider_id or None)
    # Rebuild HDHR playlist from new channel source in background
    from m3u_service import run_hdhr_playlist_build
    background_tasks.add_task(run_hdhr_playlist_build)
    return RedirectResponse(url="/hdhomerun?success=Provider+selection+saved", status_code=303)


@router.post("/hdhomerun", response_class=RedirectResponse)
async def handle_hdhomerun_form(
    request: Request,
    background_tasks: BackgroundTasks,
    save_filters: str = Form(None),
    new_includes: str = Form(None),
    epg_time_offset_hours: str = Form(None),
    db: Session = Depends(get_db),
):
    # Save EPG time offset whenever submitted (independent of save_filters gate)
    if epg_time_offset_hours is not None:
        try:
            offset_val = int(epg_time_offset_hours)
            set_app_config(db, "epg_time_offset_hours", str(offset_val))
        except ValueError:
            pass

    if save_filters:
        if new_includes and '\n' in new_includes:
            new_includes = ','.join([inc.strip() for inc in new_includes.split('\n') if inc.strip()])
        new_includes = new_includes or ""

        set_app_config(db, "hdhr_includes", new_includes or None)

        try:
            from m3u_service import build_hdhr_playlist
            kept, raw_total = await asyncio.to_thread(build_hdhr_playlist)

            if not new_includes:
                return RedirectResponse(
                    url="/hdhomerun?error=Channel list is empty — add channels in the format: 100|ESPN",
                    status_code=303,
                )
            if kept == 0:
                return RedirectResponse(
                    url="/hdhomerun?error=Filters saved but no channels matched — check your filter list (fetch M3U first if needed)",
                    status_code=303,
                )

            background_tasks.add_task(_do_epg_refresh)
            success_msg = urllib.parse.quote(f"Filters saved — {kept} of {raw_total} channels matched")
            return RedirectResponse(url=f"/hdhomerun?success={success_msg}", status_code=303)
        except Exception as e:
            logger.error(f"Failed to build HDHR playlist after save: {e}")
            return RedirectResponse(
                url=f"/hdhomerun?error=Filters saved but playlist build failed: {urllib.parse.quote(str(e))}",
                status_code=303,
            )

    return RedirectResponse(url="/hdhomerun", status_code=303)


@router.get("/epg/channel-map", response_class=HTMLResponse)
async def epg_channel_map_page(request: Request, error: str = None, success: str = None):
    """Channel → EPG source ID mapping override page."""
    import json as _json
    from hdhomerun_routes import load_channel_lineup

    def _load():
        with SessionLocal() as db:
            channels = load_channel_lineup(db)
            existing_map = get_epg_channel_map(db)
        matches_path = EPG_CACHE_PATH + ".matches.json"
        try:
            with open(matches_path, 'r', encoding='utf-8') as f:
                match_results = _json.load(f)
        except (FileNotFoundError, ValueError):
            match_results = {}
        return channels, existing_map, match_results

    channels, existing_map, match_results = await asyncio.to_thread(_load)

    template = templates.get_template("epg_channel_map.html")
    rendered = template.render({
        "request": request,
        "channels": channels,
        "existing_map": existing_map,
        "match_results": match_results,
        "active_page": "hdhomerun",
        "error": error,
        "success": success,
    })
    return HTMLResponse(content=rendered)


@router.post("/epg/channel-map", response_class=RedirectResponse)
async def epg_channel_map_save(request: Request, db: Session = Depends(get_db)):
    """Save explicit tvg-id → EPG source ID mappings."""
    form = await request.form()
    mappings: dict[str, str] = {}
    for key, value in form.items():
        if key.startswith("epg_src_"):
            tvg_id = key[len("epg_src_"):]
            epg_src = (value or "").strip()
            if tvg_id and epg_src:
                mappings[tvg_id] = epg_src
    save_epg_channel_map(db, mappings)
    _do_epg_refresh()
    return RedirectResponse(url="/epg/channel-map?success=EPG+mappings+saved+%E2%80%94+rebuilding+guide",
                            status_code=303)


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
async def tools_page(request: Request, error: str = None, success: str = None):
    import vpn_manager
    base_url = get_base_url(request)

    def _load():
        with SessionLocal() as db:
            item = get_item_context(db, base_url, config.M3U_DIR)
            all_items = db.query(Item).all()
            provider_count = len(all_items)
            mqtt_device_count = sum(
                1 for it in all_items if it.mqtt_topic_prefix and it.mqtt_topic_prefix.strip()
            )
            hdhr_provider_id = get_app_config(db, "hdhr_provider_id")
            hdhr_stream_provider_id = get_app_config(db, "hdhr_stream_provider_id")
            hdhr_selected_name = None
            if hdhr_stream_provider_id:
                try:
                    sel = db.query(Item).filter(Item.id == int(hdhr_stream_provider_id)).first()
                    if sel:
                        hdhr_selected_name = sel.name
                except (TypeError, ValueError):
                    pass
        return item, provider_count, mqtt_device_count, hdhr_selected_name

    item, provider_count, mqtt_device_count, hdhr_selected_name = await asyncio.to_thread(_load)
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
        "hdhr_selected_name": hdhr_selected_name,
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
        ok, msg = await asyncio.to_thread(_mqtt.start_mqtt, cfg)
        if not ok:
            return RedirectResponse(
                url=f"{redirect_base}?error={urllib.parse.quote('MQTT saved but connect failed: ' + msg)}",
                status_code=303,
            )
    else:
        await asyncio.to_thread(_mqtt.stop_mqtt)
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
async def providers_list(request: Request, error: str = None, success: str = None):
    base_url = get_base_url(request)

    def _load():
        with SessionLocal() as db:
            return get_all_item_contexts(db, base_url, config.M3U_DIR)

    items = await asyncio.to_thread(_load)
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
    proxy_username = proxy_username.strip() or "iptv"
    proxy_password = proxy_password.strip() or "iptv"
    conflict = db.query(Item).filter(
        Item.proxy_username == proxy_username,
        Item.proxy_password == proxy_password,
    ).first()
    if conflict:
        return RedirectResponse(
            url=f"/providers/new?error=Proxy+credentials+'{proxy_username}'/'{proxy_password}'+already+used+by+'{conflict.name}'",
            status_code=303,
        )
    result = create_item(
        db, name, server_url, username, user_pass,
        languages=None, includes=None, excludes=None,
        m3u_refresh_hours=m3u_refresh_hours, max_sessions=max_sessions,
        slug=slug,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
    )
    if not result:
        return RedirectResponse(url="/providers/new?error=Failed+to+create+provider", status_code=303)
    return RedirectResponse(url=f"/providers/{result.id}?success=Provider+created+successfully", status_code=303)


@router.get("/providers/{item_id}", response_class=HTMLResponse)
async def provider_edit_form(
    item_id: int,
    request: Request,
    error: str = None,
    success: str = None,
):
    base_url = get_base_url(request)

    def _load():
        with SessionLocal() as db:
            db_item = db.query(Item).filter(Item.id == item_id).first()
            if not db_item:
                return None
            return get_item_context(db, base_url, config.M3U_DIR, db_item)

    item = await asyncio.to_thread(_load)
    if not item:
        return RedirectResponse(url="/providers?error=Provider+not+found", status_code=302)

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
    proxy_username = proxy_username.strip() or "iptv"
    proxy_password = proxy_password.strip() or "iptv"
    conflict = db.query(Item).filter(
        Item.proxy_username == proxy_username,
        Item.proxy_password == proxy_password,
        Item.id != item_id,
    ).first()
    if conflict:
        return RedirectResponse(
            url=f"/providers/{item_id}?error=Proxy+credentials+'{proxy_username}'/'{proxy_password}'+already+used+by+'{conflict.name}'",
            status_code=303,
        )
    result = update_item(
        db, item_id,
        name=name, slug=slug, server_url=server_url,
        username=username, user_pass=user_pass,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
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

    # Ensure IPTV-app (Xtream) responses rebuild after filter changes.
    try:
        from xtream_server_routes import invalidate_xtream_cache
        invalidate_xtream_cache(item_id)
    except Exception:
        pass

    m3u_path = os.path.join(config.M3U_DIR, f"xtream_playlist_{item_id}.m3u")
    if not os.path.exists(m3u_path):
        return RedirectResponse(
            url=f"/providers/{item_id}?success=Filters+saved+%E2%80%94+fetch+M3U+first+to+apply",
            status_code=303,
        )

    try:
        configured = bool(result.includes or result.excludes or result.languages or result.xtream_includes)
        if configured:
            queue_refresh_filtered(item_id)
            background_tasks.add_task(_do_epg_refresh)
            return RedirectResponse(
                url=f"/providers/{item_id}?success=Filters+saved+%E2%80%94+applying+in+background",
                status_code=303,
            )

        return RedirectResponse(
            url=f"/providers/{item_id}?success=Filters+saved",
            status_code=303,
        )
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

