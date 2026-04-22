from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates
import time
from sqlalchemy.orm import Session
from models import get_db, Item
from services import create_item, update_item, delete_item, get_all_items
import config
import logging
import os
import re
from hdhomerun_routes import hdhomerun_emulator
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
    items = get_all_items(db)
    items_with_files = []
    base_url = get_base_url(request)

    # Do a single directory listing instead of checking files individually
    try:
        existing_files = set(os.listdir(config.M3U_DIR))
    except Exception as e:
        logger.error(f"Error listing m3u_files directory: {e}")
        existing_files = set()

    for item in items:
        item_dict = item.__dict__
        # Check against our cached file listing instead of doing os.path.exists
        item_dict['has_m3u'] = f"xtream_playlist_{item.id}.m3u" in existing_files
        item_dict['has_filtered'] = f"filtered_playlist_{item.id}.m3u" in existing_files
        item_dict['has_epg'] = f"filtered_epg_{item.id}.xml" in existing_files
        item_dict['stream_url'] = f"{base_url}/stream_filtered_m3u/{item.id}"
        item_dict['epg_url'] = f"{base_url}/epg.xml"
        item_dict['m3u_refresh_hours'] = int(item.m3u_refresh_hours or 0)
        # Last fetch time from file mtime
        m3u_path = os.path.join(config.M3U_DIR, f"xtream_playlist_{item.id}.m3u")
        try:
            mtime = os.path.getmtime(m3u_path)
            item_dict['m3u_last_fetched_ts'] = int(mtime)
        except FileNotFoundError:
            item_dict['m3u_last_fetched_ts'] = None
        items_with_files.append(item_dict)

    # Determine if SSDP discovery can be safely enabled
    # SSDP is disabled by default on macOS (HDHR_DISABLE_SSDP=1) to prevent 4-5 minute hangs
    # If the env var is set to 1, we're likely on macOS and should show the warning
    ssdp_disabled_by_env = hdhomerun_emulator.is_env_disabled()
    can_enable_ssdp = not ssdp_disabled_by_env  # Can only enable if not disabled by env
    
    env_pairs = [
        ("HDHomeRun URL", base_url),
        ("Tuners", str(config.HDHR_TUNER_COUNT)),
        ("EPG XML", f"{base_url}/epg.xml"),
        ("Xtream Server", base_url),
        ("Xtream User", config.IPTV_USERNAME),
        ("Xtream Pass", config.IPTV_PASSWORD),
    ]
    friendly_name      = config.HDHR_FRIENDLY_NAME
    allow_full_m3u_download = config.ALLOW_FULL_M3U_DOWNLOAD
    iptv_username      = config.IPTV_USERNAME
    iptv_password      = config.IPTV_PASSWORD

    context = {
        "request": request,
        "items": items_with_files,
        "error": error,
        "success": success,
        "base_url": base_url,
        "hdhr_running": hdhomerun_emulator.is_running(),
        "can_enable_ssdp": can_enable_ssdp,
        "ssdp_disabled_by_env": ssdp_disabled_by_env,
        "env_pairs": env_pairs,
        "friendly_name": friendly_name,
        "allow_full_m3u_download": allow_full_m3u_download,
        "iptv_username": iptv_username,
        "iptv_password": iptv_password,
    }

    # Render template to measure rendering time (helps diagnose hangs)
    start = time.perf_counter()
    template = templates.get_template("index.html")
    rendered = template.render(context)
    render_duration = time.perf_counter() - start
    logger.info(f"Template render duration: {render_duration:.3f}s")

    return HTMLResponse(content=rendered)

@router.post("/", response_class=RedirectResponse)
async def handle_form(
    request: Request,
    add: str = Form(None),
    edit: str = Form(None),
    delete: str = Form(None),
    name: str = Form(None),
    server_url: str = Form(None),
    username: str = Form(None),
    user_pass: str = Form(None),
    languages: str = Form(None),
    includes: str = Form(None),
    excludes: str = Form(None),
    item_id: int = Form(None),
    new_name: str = Form(None),
    new_server_url: str = Form(None),
    new_username: str = Form(None),
    new_user_pass: str = Form(None),
    new_languages: str = Form(None),
    new_includes: str = Form(None),
    new_excludes: str = Form(None),
    new_xtream_includes: str = Form(None),
    db: Session = Depends(get_db)
):
    #logger.info(f"Received form data: add={add}, edit={edit}, delete={delete}, name='{name}', server_url='{server_url}', username='{username}', user_pass='{user_pass}', languages='{languages}', includes='{includes}', excludes='{excludes}', guide_ids='{guide_ids}', item_id={item_id}, new_name='{new_name}', new_server_url='{new_server_url}', new_username='{new_username}', new_user_pass='{new_user_pass}', new_languages='{new_languages}', new_includes='{new_includes}', new_excludes='{new_excludes}', new_guide_ids='{new_guide_ids}'")
    
    # Convert newline-separated values to comma-separated for storage
    if languages and '\n' in languages:
        languages = ','.join([lang.strip() for lang in languages.split('\n') if lang.strip()])
    if includes and '\n' in includes:
        includes = ','.join([inc.strip() for inc in includes.split('\n') if inc.strip()])
    if excludes and '\n' in excludes:
        excludes = ','.join([ex.strip() for ex in excludes.split('\n') if ex.strip()])
    if new_languages and '\n' in new_languages:
        new_languages = ','.join([lang.strip() for lang in new_languages.split('\n') if lang.strip()])
    if new_includes and '\n' in new_includes:
        new_includes = ','.join([inc.strip() for inc in new_includes.split('\n') if inc.strip()])
    if new_excludes and '\n' in new_excludes:
        new_excludes = ','.join([ex.strip() for ex in new_excludes.split('\n') if ex.strip()])
    if new_xtream_includes and '\n' in new_xtream_includes:
        new_xtream_includes = ','.join([x.strip() for x in new_xtream_includes.split('\n') if x.strip()])
    if add:
        logger.info(f"Processing add request with name: '{name}'")
        result = create_item(db, name, server_url, username, user_pass, languages, includes, excludes)
        if not result:
            logger.warning("Item creation failed")
            return RedirectResponse(url="/?error=Failed to create item", status_code=303)
        # Purge any stale files from a previous config that had the same ID (SQLite ID reuse)
        for fname in [
            f"xtream_playlist_{result.id}.m3u",
            f"filtered_playlist_{result.id}.m3u",
            f"epg_{result.id}.xml",
            f"filtered_epg_{result.id}.xml",
        ]:
            fpath = os.path.join(config.M3U_DIR, fname)
            try:
                if os.path.exists(fpath):
                    os.remove(fpath)
                    logger.info(f"Removed stale {fname} on new config creation")
            except Exception as e:
                logger.warning(f"Could not remove stale {fname}: {e}")
    elif edit or (item_id and new_name and new_server_url and new_username and new_user_pass and not add and not delete):
        logger.info(f"Processing edit request for item {item_id}")
        if not item_id or not all([new_name, new_server_url, new_username, new_user_pass]):
            logger.warning(f"Missing item_id or fields for edit: item_id={item_id}")
            return RedirectResponse(url="/?error=Missing item ID or fields", status_code=303)
        if not update_item(db, item_id, new_name, new_server_url, new_username, new_user_pass, new_languages, new_includes, new_excludes, new_xtream_includes):
            logger.warning(f"Item update failed for id {item_id}")
            return RedirectResponse(url="/?error=Item not found", status_code=303)
        # Only invalidate filtered playlist if filter-relevant fields changed.
        # Changing name/URL/credentials/refresh schedule does not affect filtering.
        filter_fields_changed = any([new_includes, new_excludes, new_languages, new_xtream_includes])
        for fname in ([f"filtered_playlist_{item_id}.m3u"] if filter_fields_changed else []):
            fpath = os.path.join(config.M3U_DIR, fname)
            try:
                if os.path.exists(fpath):
                    os.remove(fpath)
                    logger.info(f"Removed {fname} after filter field change")
            except Exception as e:
                logger.warning(f"Could not remove {fname}: {e}")
    elif delete:
        logger.info(f"Processing delete request for item {item_id}")
        if not item_id:
            logger.warning(f"Missing item_id for delete: item_id={item_id}")
            return RedirectResponse(url="/?error=Missing item ID", status_code=303)
        if not delete_item(db, item_id):
            logger.warning(f"Item deletion failed for id {item_id}")
            return RedirectResponse(url="/?error=Item not found", status_code=303)
        # Cleanup all files associated with this config
        for fname in [
            f"xtream_playlist_{item_id}.m3u",
            f"filtered_playlist_{item_id}.m3u",
            f"epg_{item_id}.xml",
            f"filtered_epg_{item_id}.xml",
        ]:
            fpath = os.path.join(config.M3U_DIR, fname)
            try:
                if os.path.exists(fpath):
                    os.remove(fpath)
                    logger.info(f"Removed {fname} after config deletion")
            except Exception as e:
                logger.warning(f"Could not remove {fname}: {e}")
    
    return RedirectResponse(url="/", status_code=303)

@router.post("/set_refresh_interval")
async def set_refresh_interval(item_id: int = Form(...), m3u_refresh_hours: int = Form(0), db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        return JSONResponse({"ok": False, "error": "Item not found"}, status_code=404)
    item.m3u_refresh_hours = m3u_refresh_hours
    db.commit()
    db.refresh(item)
    logger.info(f"Set m3u_refresh_hours={item.m3u_refresh_hours} for item {item_id}")
    return JSONResponse({"ok": True, "m3u_refresh_hours": item.m3u_refresh_hours})


@router.post("/generate_m3u", response_class=RedirectResponse)
async def generate_m3u(background_tasks: BackgroundTasks, item_id: int = Form(...), db: Session = Depends(get_db)):
    try:
        ok, msg, _ = do_fetch_m3u(item_id, db)
        if not ok:
            return RedirectResponse(url=f"/?error={urllib.parse.quote(msg)}", status_code=303)
        background_tasks.add_task(_refresh_epg, True)
        logger.info("EPG rebuild queued in background after M3U save")
        return RedirectResponse(url=f"/?success={urllib.parse.quote(msg)}", status_code=303)
    except Exception as e:
        logger.error(f"Failed to generate M3U for item {item_id}: {e}")
        return RedirectResponse(url=f"/?error=Failed to save M3U file: {urllib.parse.quote(str(e))}", status_code=303)

@router.post("/generate_filtered_m3u", response_class=RedirectResponse)
async def generate_filtered_m3u(background_tasks: BackgroundTasks, item_id: int = Form(...), db: Session = Depends(get_db)):
    try:
        item = db.query(Item).filter(Item.id == item_id).first()
        if not item:
            logger.warning(f"Item with id {item_id} not found for filtered M3U generation")
            return RedirectResponse(url="/?error=Item not found", status_code=303)

        m3u_path = os.path.join(config.M3U_DIR, f"xtream_playlist_{item_id}.m3u")
        if not os.path.exists(m3u_path):
            logger.warning(f"M3U file not found for item {item_id} at {m3u_path}")
            return RedirectResponse(url="/?error=M3U file not found, fetch M3U first", status_code=303)

        with open(m3u_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()

        languages, includes_map, raw_includes, excludes, has_wildcard = build_filter_config(item)
        logger.info(
            f"Filtering item {item_id}: languages={languages} includes={raw_includes} "
            f"excludes={excludes} wildcard={has_wildcard}"
        )

        filtered_content, num_records, input_record_count = apply_m3u_filter(
            lines, languages, includes_map, excludes, has_wildcard
        )

        if num_records == 0:
            logger.warning(f"No records matched filter for item {item_id}")
            return RedirectResponse(url="/?error=No records matched the filter criteria.", status_code=303)

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
        logger.info("EPG rebuild queued in background after filtered M3U save")
        return RedirectResponse(url=f"/?success={success_msg}", status_code=303)

    except Exception as e:
        logger.error(f"Failed to generate filtered M3U for item {item_id}: {str(e)}")
        return RedirectResponse(url=f"/?error=Failed to save filtered M3U file: {str(e)}", status_code=303)

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

