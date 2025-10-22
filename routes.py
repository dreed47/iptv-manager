from fastapi import APIRouter, Depends, HTTPException, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates
import time
from sqlalchemy.orm import Session
from models import get_db, Item
from services import create_item, update_item, delete_item, get_all_items
import logging
import os
from hdhomerun_routes import hdhomerun_emulator
import urllib.parse
import requests
import json
import re
import xml.etree.ElementTree as ET

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

def get_base_url(request: Request) -> str:
    """Get the base URL including protocol and host"""
    # Use environment variables or fallback to the configured IP
    host = os.getenv("HDHR_ADVERTISE_HOST", "192.168.86.254")
    port = os.getenv("HDHR_ADVERTISE_PORT", "5005")
    scheme = os.getenv("HDHR_SCHEME", "http")
    return f"{scheme}://{host}:{port}"

@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db), error: str = None, success: str = None):
    items = get_all_items(db)
    items_with_files = []
    base_url = get_base_url(request)

    # Do a single directory listing instead of checking files individually
    try:
        existing_files = set(os.listdir("/app/m3u_files"))
    except Exception as e:
        logger.error(f"Error listing m3u_files directory: {e}")
        existing_files = set()

    for item in items:
        item_dict = item.__dict__
        # Check against our cached file listing instead of doing os.path.exists
        item_dict['has_m3u'] = f"xtream_playlist_{item.id}.m3u" in existing_files
        item_dict['has_filtered'] = f"filtered_playlist_{item.id}.m3u" in existing_files
        item_dict['has_epg'] = f"filtered_epg_{item.id}.xml" in existing_files
        # Add streaming URLs to the item
        item_dict['stream_url'] = f"{base_url}/stream_filtered_m3u/{item.id}"
        item_dict['epg_url'] = f"{base_url}/stream_epg/{item.id}"
        items_with_files.append(item_dict)

    # Determine if SSDP discovery can be safely enabled
    # SSDP is disabled by default on macOS (HDHR_DISABLE_SSDP=1) to prevent 4-5 minute hangs
    # If the env var is set to 1, we're likely on macOS and should show the warning
    ssdp_disabled_by_env = hdhomerun_emulator.is_env_disabled()
    can_enable_ssdp = not ssdp_disabled_by_env  # Can only enable if not disabled by env
    
    context = {
        "request": request,
        "items": items_with_files,
        "error": error,
        "success": success,
        "base_url": base_url,
        "hdhr_running": hdhomerun_emulator.is_running(),
        "can_enable_ssdp": can_enable_ssdp,
        "ssdp_disabled_by_env": ssdp_disabled_by_env,
    }

    # Render template to measure rendering time (helps diagnose hangs)
    start = time.time()
    template = templates.get_template("index.html")
    rendered = template.render(context)
    render_duration = time.time() - start
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
    epg_channels: str = Form(None),  
    item_id: int = Form(None),
    new_name: str = Form(None),
    new_server_url: str = Form(None),
    new_username: str = Form(None),
    new_user_pass: str = Form(None),
    new_languages: str = Form(None),
    new_includes: str = Form(None),
    new_excludes: str = Form(None),
    new_epg_channels: str = Form(None),  
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
    if epg_channels and '\n' in epg_channels:
        epg_channels = ','.join([chan.strip() for chan in epg_channels.split('\n') if chan.strip()])
    if new_epg_channels and '\n' in new_epg_channels:
        new_epg_channels = ','.join([chan.strip() for chan in new_epg_channels.split('\n') if chan.strip()])
            
    if add:
        logger.info(f"Processing add request with name: '{name}'")
        result = create_item(db, name, server_url, username, user_pass, languages, includes, excludes, epg_channels)
        if not result:
            logger.warning("Item creation failed")
            return RedirectResponse(url="/?error=Failed to create item", status_code=303)
    elif edit or (item_id and new_name and new_server_url and new_username and new_user_pass and not add and not delete):
        logger.info(f"Processing edit request for item {item_id}")
        if not item_id or not all([new_name, new_server_url, new_username, new_user_pass]):
            logger.warning(f"Missing item_id or fields for edit: item_id={item_id}")
            return RedirectResponse(url="/?error=Missing item ID or fields", status_code=303)
        if not update_item(db, item_id, new_name, new_server_url, new_username, new_user_pass, new_languages, new_includes, new_excludes, new_epg_channels):
            logger.warning(f"Item update failed for id {item_id}")
            return RedirectResponse(url="/?error=Item not found", status_code=303)
    elif delete:
        logger.info(f"Processing delete request for item {item_id}")
        if not item_id:
            logger.warning(f"Missing item_id for delete: item_id={item_id}")
            return RedirectResponse(url="/?error=Missing item ID", status_code=303)
        if not delete_item(db, item_id):
            logger.warning(f"Item deletion failed for id {item_id}")
            return RedirectResponse(url="/?error=Item not found", status_code=303)
    
    return RedirectResponse(url="/", status_code=303)

@router.post("/generate_m3u", response_class=RedirectResponse)
async def generate_m3u(item_id: int = Form(...), db: Session = Depends(get_db)):
    try:
        item = db.query(Item).filter(Item.id == item_id).first()
        if not item:
            logger.warning(f"Item with id {item_id} not found for M3U generation")
            return RedirectResponse(url="/?error=Item not found", status_code=303)
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": item.server_url.rstrip('/'),
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive"
        }
        
        base_url = f"{item.server_url.rstrip('/')}/player_api.php"
        auth_url = f"{base_url}?username={urllib.parse.quote(item.username)}&password={urllib.parse.quote(item.user_pass)}"
        logger.info(f"Attempting Xtream API auth: {auth_url}")
        
        m3u_content = None
        num_records = 0
        source = "Xtream API"
        try:
            response = requests.get(auth_url, headers=headers, timeout=30)
            response.raise_for_status()
            user_data = response.json()
            logger.info(f"Xtream API auth response: {json.dumps(user_data, indent=2)[:500]}")
            
            if user_data.get('user_info', {}).get('auth', 0) != 1:
                logger.warning(f"Invalid Xtream Codes credentials for item {item_id}")
                raise ValueError("Invalid credentials")
            
            logger.info(f"Authenticated with Xtream Codes for user {item.username}")
            
            live_streams_url = f"{auth_url}&action=get_live_streams"
            live_streams_response = requests.get(live_streams_url, headers=headers, timeout=30)
            live_streams_response.raise_for_status()
            live_streams = live_streams_response.json()
            
            vod_streams_url = f"{auth_url}&action=get_vod_streams"
            vod_streams_response = requests.get(vod_streams_url, headers=headers, timeout=30)
            vod_streams_response.raise_for_status()
            vod_streams = vod_streams_response.json()
            
            series_url = f"{auth_url}&action=get_series"
            series_response = requests.get(series_url, headers=headers, timeout=30)
            series_response.raise_for_status()
            series = series_response.json()
            
            num_records = len(live_streams) + len(vod_streams) + len(series)
            logger.info(f"Fetched {len(live_streams)} live streams, {len(vod_streams)} VOD streams, {len(series)} series (total: {num_records})")
            
            m3u_content = "#EXTM3U\n"
            for stream in live_streams:
                stream_id = stream.get('stream_id')
                name = stream.get('name', 'Unknown')
                stream_url = f"{item.server_url.rstrip('/')}/live/{item.username}/{item.user_pass}/{stream_id}.ts"
                m3u_content += f"#EXTINF:-1 tvg-id=\"{stream.get('stream_id', '')}\" tvg-name=\"{name}\" tvg-logo=\"{stream.get('stream_icon', '')}\" group-title=\"{stream.get('category_name', 'Live')}\", {name}\n{stream_url}\n"
            
            for stream in vod_streams:
                stream_id = stream.get('stream_id')
                name = stream.get('name', 'Unknown')
                stream_url = f"{item.server_url.rstrip('/')}/movie/{item.username}/{item.user_pass}/{stream_id}.mp4"
                m3u_content += f"#EXTINF:-1 tvg-id=\"{stream.get('stream_id', '')}\" tvg-name=\"{name}\" tvg-logo=\"{stream.get('stream_icon', '')}\" group-title=\"{stream.get('category_name', 'VOD')}\", {name}\n{stream_url}\n"
            
            for serie in series:
                series_id = serie.get('series_id')
                name = serie.get('name', 'Unknown')
                stream_url = f"{item.server_url.rstrip('/')}/series/{item.username}/{item.user_pass}/{series_id}.m3u8"
                m3u_content += f"#EXTINF:-1 tvg-id=\"{series_id}\" tvg-name=\"{name}\" tvg-logo=\"{serie.get('cover', '')}\" group-title=\"Series\", {name}\n{stream_url}\n"
        
        except (requests.exceptions.RequestException, ValueError, json.JSONDecodeError) as e:
            logger.warning(f"Xtream API failed for item {item_id}: {str(e)}, falling back to M3U URL")
            source = "M3U URL"
            
            m3u_url = f"{item.server_url.rstrip('/')}/get.php?username={urllib.parse.quote(item.username)}&password={urllib.parse.quote(item.user_pass)}&type=m3u_plus&output=ts"
            logger.info(f"Attempting M3U fetch from: {m3u_url}")
            
            for attempt in range(3):
                try:
                    response = requests.get(m3u_url, headers=headers, timeout=30)
                    response.raise_for_status()
                    m3u_content = response.text
                    logger.info(f"M3U response status: {response.status_code}, headers: {response.headers}")
                    logger.debug(f"M3U response content (first 200 chars): {m3u_content[:200]}")
                    break
                except requests.exceptions.RequestException as e:
                    logger.warning(f"Attempt {attempt + 1} failed for {m3u_url}: {str(e)}")
                    if attempt == 2:
                        logger.error(f"Failed to fetch M3U for item {item_id}: {str(e)}, response text: {getattr(e.response, 'text', 'No response text')[:500]}")
                        return RedirectResponse(url=f"/?error=Failed to fetch M3U: {str(e)}", status_code=303)
            
            if not m3u_content.startswith("#EXTM3U"):
                logger.warning(f"Invalid M3U content received for item {item_id}: {m3u_content[:100]}")
                return RedirectResponse(url="/?error=Invalid M3U content from provider", status_code=303)
            
            num_records = len(re.findall(r'^#EXTINF', m3u_content, re.MULTILINE))
        
        output_dir = "/app/m3u_files"
        os.makedirs(output_dir, exist_ok=True)
        m3u_file_path = os.path.join(output_dir, f"xtream_playlist_{item_id}.m3u")
        with open(m3u_file_path, "w", encoding="utf-8") as f:
            f.write(m3u_content)
        
        total_lines = len(m3u_content.splitlines())
        logger.info(f"Generated and saved {source} playlist for item {item_id} ({num_records} records, {total_lines} lines) at {m3u_file_path}")

        # Filter the M3U file based on languages/includes/excludes
        languages = [lang.strip() for lang in (item.languages or "").split(",") if lang.strip()]
        includes = [inc.strip() for inc in (item.includes or "").split(",") if inc.strip()]
        excludes = [exc.strip() for exc in (item.excludes or "").split(",") if exc.strip()]

        logger.info(f"Starting M3U filtering process...")
        logger.info(f"Filter settings - Languages: {languages}, Includes: {includes}, Excludes: {excludes}")
        
        if includes or excludes or languages:
            has_wildcard_exclude = "*" in excludes
            logger.info(f"Filtering M3U with languages={languages}, includes={includes}, excludes={excludes}, wildcard_exclude={has_wildcard_exclude}")
            
            filtered_content = "#EXTM3U\n"
            lines = m3u_content.splitlines()
            num_filtered = 0
            i = 1 if lines and lines[0].strip() == "#EXTM3U" else 0
            
            while i < len(lines):
                if lines[i].startswith("#EXTINF"):
                    if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                        extinf = lines[i]
                        url = lines[i + 1]
                        
                        # Parse EXTINF attributes and channel name
                        if "," in extinf:
                            _, channel_name = extinf.split(",", 1)
                            channel_name = channel_name.strip()
                            
                            # Start with channel included
                            include = True
                            
                            # Handle wildcard exclude with includes
                            if has_wildcard_exclude:
                                # If we have wildcard exclude, start with excluded
                                include = False
                                # Only include if it exactly matches an include
                                if includes:
                                    include = any(inc.lower() == channel_name.lower() for inc in includes)
                                    if include:
                                        logger.debug(f"Wildcard override - exact match: '{channel_name}'")
                            # Handle normal filtering
                            elif includes:
                                # If we have includes, only keep exact matches
                                include = any(inc.lower() == channel_name.lower() for inc in includes)
                                if include:
                                    logger.debug(f"Include match: '{channel_name}'")
                            elif excludes:
                                # Only apply excludes if no includes specified
                                include = not any(exc.lower() in channel_name.lower() for exc in excludes)
                            
                            if include:
                                filtered_content += f"{extinf}\n{url}\n"
                                num_filtered += 1
                                logger.info(f"Kept channel: {channel_name}")
                            else:
                                logger.debug(f"Filtered out: {channel_name}")
                    i += 2
                else:
                    i += 1
            
            # Save filtered M3U
            filtered_path = os.path.join(output_dir, f"filtered_playlist_{item_id}.m3u")
            logger.info(f"Attempting to save filtered M3U to: {filtered_path}")
            try:
                with open(filtered_path, "w", encoding="utf-8") as f:
                    f.write(filtered_content)
                logger.info(f"Successfully saved filtered playlist with {num_filtered} channels (reduced from {num_records})")
                
                # Verify the file exists and has content
                if os.path.exists(filtered_path):
                    file_size = os.path.getsize(filtered_path)
                    logger.info(f"Verified filtered file exists: {filtered_path} (size: {file_size} bytes)")
                else:
                    logger.error(f"Failed to verify filtered file at: {filtered_path}")
                    
                num_records = num_filtered
            except Exception as e:
                logger.error(f"Failed to save filtered M3U: {str(e)}")
        
        epg_error = None
        epg_url = f"{item.server_url.rstrip('/')}/xmltv.php?username={urllib.parse.quote(item.username)}&password={urllib.parse.quote(item.user_pass)}"
        try:
            epg_response = requests.get(epg_url, headers=headers, timeout=30)
            epg_response.raise_for_status()
            epg_file_path = os.path.join(output_dir, f"epg_{item_id}.xml")
            with open(epg_file_path, "w", encoding="utf-8") as f:
                f.write(epg_response.text)
            logger.info(f"Saved EPG for item {item_id} at {epg_file_path}")
        except requests.exceptions.RequestException as e:
            epg_error = f"Failed to fetch EPG: {str(e)}"
            logger.warning(epg_error)
        
        redirect_url = f"/?success=Saved {num_records} records ({total_lines} lines) to M3U file from {source}"
        if epg_error:
            redirect_url += f"&error={urllib.parse.quote(epg_error)}"
        
        return RedirectResponse(url=redirect_url, status_code=303)
    
    except Exception as e:
        logger.error(f"Failed to generate M3U for item {item_id}: {str(e)}")
        return RedirectResponse(url=f"/?error=Failed to save M3U file: {str(e)}", status_code=303)

@router.post("/generate_filtered_m3u", response_class=RedirectResponse)
async def generate_filtered_m3u(item_id: int = Form(...), db: Session = Depends(get_db)):

    try:
        item = db.query(Item).filter(Item.id == item_id).first()
        if not item:
            logger.warning(f"Item with id {item_id} not found for filtered M3U generation")
            return RedirectResponse(url="/?error=Item not found", status_code=303)

        m3u_path = os.path.join("/app/m3u_files", f"xtream_playlist_{item_id}.m3u")
        if not os.path.exists(m3u_path):
            logger.warning(f"M3U file not found for item {item_id} at {m3u_path}")
            return RedirectResponse(url="/?error=M3U file not found, fetch M3U first", status_code=303)

        with open(m3u_path, "r", encoding="utf-8") as f:
            m3u_content = f.read()

        languages = [lang.strip().lower() for lang in (item.languages or "").split(",") if lang.strip()]
        # Normalization helper used for includes/excludes and matching
        import unicodedata
        def normalize(s):
            s = s.lower().strip()
            s = unicodedata.normalize('NFKD', s)
            s = ''.join(c for c in s if not unicodedata.combining(c))
            s = ' '.join(s.split())
            return s
        # Stricter normalization for includes exact matching: remove non-alphanumerics
        def strict_normalize(s):
            t = normalize(s)
            return re.sub(r'[^a-z0-9]+', '', t)

        # Build an includes map for exact name matching (robust normalization): normalized_name -> channel_number (or None)
        includes_map = {}
        raw_includes = []
        for inc in (item.includes or "").split(","):
            inc = inc.strip()
            if not inc:
                continue
            if '|' in inc:
                num, name = inc.split('|', 1)
                raw_includes.append((num.strip(), name.strip()))
                includes_map[strict_normalize(name)] = num.strip()
            else:
                raw_includes.append((None, inc.strip()))
                includes_map[strict_normalize(inc)] = None
        excludes = [ex.strip().lower() for ex in (item.excludes or "").split(",") if ex.strip()]
        has_wildcard_exclude = "*" in excludes

        logger.info(
            f"Filtering item {item_id} with languages={languages}, includes={raw_includes}, excludes={excludes}, wildcard_exclude={has_wildcard_exclude}"
        )

        filtered_content = "#EXTM3U\n"
        lines = m3u_content.splitlines()
        # Count input records (#EXTINF entries) for reporting
        input_record_count = sum(1 for ln in lines if ln.startswith("#EXTINF"))
        num_records = 0
        i = 0
        if lines and lines[0].strip() == "#EXTM3U":
            i = 1

        while i < len(lines):
            if lines[i].startswith("#EXTINF") and i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                extinf = lines[i]
                url = lines[i + 1]

                # Parse channel name and tvg-name
                attributes = {}
                channel_name = ""
                if " " in extinf and "," in extinf:
                    attr_part, channel_name = extinf.split(",", 1)
                    attr_matches = re.findall(r'(\S+?)="([^"]*)"', attr_part)
                    for key, value in attr_matches:
                        attributes[key.lower()] = value.lower()
                else:
                    channel_name = extinf.split(",", 1)[1] if "," in extinf else ""
                tvg_name = attributes.get('tvg-name', '')
                channel_language = ""
                if " - " in tvg_name:
                    # Standard format: "EN - Channel Name"
                    channel_language = tvg_name.split(" - ")[0].strip().lower()
                elif languages:
                    # No standard separator - check if any language appears at start of tvg_name
                    tvg_lower = tvg_name.lower()
                    for lang in languages:
                        if tvg_lower.startswith(lang.lower() + ":") or tvg_lower.startswith(lang.lower() + " "):
                            channel_language = lang.lower()
                            break
                    # If still no match, don't filter by language for this channel
                    # (allow includes to work)

                # 1. Language check (only if we found a valid language prefix)
                if languages and channel_language and channel_language not in languages:
                    i += 2
                    continue

                # 2. Exclude logic
                search_text = normalize(f"{tvg_name} {channel_name}")
                excluded = False
                if has_wildcard_exclude:
                    # Exclude all unless included
                    excluded = True
                else:
                    for ex in excludes:
                        if ex and normalize(ex) in search_text:
                            excluded = True
                            break

                # 3. Include logic (allow-list when provided): exact match after normalization
                included = False
                chno_to_apply = None
                if includes_map:
                    # Compare against both provided channel_name and tvg_name for exact match (normalized)
                    key_candidates = [strict_normalize(channel_name), strict_normalize(tvg_name)]
                    for cand in key_candidates:
                        if cand in includes_map:
                            included = True
                            chno_to_apply = includes_map[cand]
                            break
                        # Allow common suffix variants like 'HD'/'4K' without enabling substring matches
                        for inc_key, num in includes_map.items():
                            if cand == inc_key + "hd" or cand == inc_key + "4k" or cand == inc_key + "fhd" or cand == inc_key + "uhd":
                                included = True
                                chno_to_apply = num
                                break
                        if included:
                            break

                # Final decision
                if includes_map:
                    # With includes present, only keep if explicitly included (and not excluded unless included)
                    if included:
                        if chno_to_apply:
                            extinf_new = re.sub(r'\s*tvg-chno="[^"]*"', '', extinf)
                            idx = extinf_new.find(',')
                            if idx != -1:
                                extinf_new = extinf_new[:idx] + f' tvg-chno="{chno_to_apply}"' + extinf_new[idx:]
                            else:
                                extinf_new = extinf_new + f' tvg-chno="{chno_to_apply}"'
                            filtered_content += f"{extinf_new}\n{url}\n"
                        else:
                            filtered_content += f"{extinf}\n{url}\n"
                        num_records += 1
                else:
                    # No includes: keep anything not excluded and matching language rules
                    if not excluded:
                        filtered_content += f"{extinf}\n{url}\n"
                        num_records += 1

                i += 2
            else:
                i += 1

        if num_records == 0:
            logger.warning(f"No records matched filter for item {item_id}: languages={item.languages}, includes={item.includes}, excludes={item.excludes}")
            return RedirectResponse(url="/?error=No records matched the filter criteria.", status_code=303)

        output_dir = "/app/m3u_files"
        os.makedirs(output_dir, exist_ok=True)
        filtered_file_path = os.path.join(output_dir, f"filtered_playlist_{item_id}.m3u")
        with open(filtered_file_path, "w", encoding="utf-8") as f:
            f.write(filtered_content)

        total_lines = len(filtered_content.splitlines())
        # Log both input and output record counts
        logger.info(
            f"Filtered M3U for item {item_id}: input records={input_record_count}, "
            f"written records={num_records}, file lines={total_lines}, path={filtered_file_path}"
        )

        # Redirect back to index with success message including counts
        success_msg = urllib.parse.quote(
            f"Filtered {num_records} of {input_record_count} records ({total_lines} lines)"
        )
        return RedirectResponse(url=f"/?success={success_msg}", status_code=303)

        #epg_success = await generate_filtered_epg(item_id, db)
        #if epg_success:
        #    return RedirectResponse(url=f"/?success=Saved {num_records} filtered records ({total_lines} lines) to filtered M3U file and generated filtered EPG", status_code=303)
        #else:
        #    return RedirectResponse(url=f"/?success=Saved {num_records} filtered records ({total_lines} lines) to filtered M3U file&error=Failed to generate filtered EPG", status_code=303)

    except Exception as e:
        logger.error(f"Failed to generate filtered M3U for item {item_id}: {str(e)}")
        return RedirectResponse(url=f"/?error=Failed to save filtered M3U file: {str(e)}", status_code=303)

async def generate_filtered_epg(item_id: int, db: Session):
    """Generate filtered EPG based on channel names provided by user"""
    try:
        item = db.query(Item).filter(Item.id == item_id).first()
        if not item:
            logger.warning(f"Item with id {item_id} not found for EPG generation")
            return False
        
        if not item.epg_channels:
            logger.warning(f"No EPG channels specified for item {item_id}")
            return False
        
        epg_path = os.path.join("/app/m3u_files", f"epg_{item_id}.xml")
        if not os.path.exists(epg_path):
            logger.warning(f"EPG file not found for item {item_id} at {epg_path}")
            return False
        
        # Parse channel names from user input
        channel_names = {name.strip() for name in item.epg_channels.split(",") if name.strip()}
        logger.info(f"Filtering EPG for {len(channel_names)} channel names: {list(channel_names)[:10]}")
        
        # Read and parse original EPG
        with open(epg_path, 'r', encoding='utf-8') as f:
            epg_content = f.read()
        
        # Parse XML
        root = ET.fromstring(epg_content)
        
        # Create new root for filtered EPG
        new_root = ET.Element('tv')
        new_root.attrib.update(root.attrib)
        
        channels_kept = 0
        programmes_kept = 0
        kept_channel_ids = set()
        
        # Find and keep matching channels
        for channel in root.findall('.//channel'):
            display_name_elem = channel.find('display-name')
            if display_name_elem is not None:
                display_name = display_name_elem.text
                if display_name and display_name in channel_names:
                    new_root.append(channel)
                    channels_kept += 1
                    kept_channel_ids.add(channel.get('id'))
                    logger.debug(f"Keeping channel: {display_name}")
        
        # Find and keep matching programmes
        for programme in root.findall('.//programme'):
            if programme.get('channel') in kept_channel_ids:
                new_root.append(programme)
                programmes_kept += 1
        
        logger.info(f"EPG filtering kept {channels_kept} channels and {programmes_kept} programmes")
        
        # Save filtered EPG
        filtered_epg_path = os.path.join("/app/m3u_files", f"filtered_epg_{item_id}.xml")
        
        with open(filtered_epg_path, 'w', encoding='utf-8') as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            f.write(ET.tostring(new_root, encoding='unicode'))
        
        return channels_kept > 0
        
    except Exception as e:
        logger.error(f"Failed to generate filtered EPG: {str(e)}")
        return False

@router.get("/download_m3u/{item_id}", response_class=FileResponse)
async def download_m3u(item_id: int, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    
    file_path = os.path.join("/app/m3u_files", f"xtream_playlist_{item_id}.m3u")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="M3U file not found")
    
    return FileResponse(file_path, filename=f"xtream_playlist_{item.name}.m3u")

@router.get("/download_filtered_m3u/{item_id}", response_class=FileResponse)
async def download_filtered_m3u(item_id: int, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    
    file_path = os.path.join("/app/m3u_files", f"filtered_playlist_{item_id}.m3u")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Filtered M3U file not found")
    
    return FileResponse(file_path, filename=f"filtered_playlist_{item.name}.m3u")

@router.get("/stream_filtered_m3u/{item_id}")
async def stream_filtered_m3u(item_id: int, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    
    file_path = os.path.join("/app/m3u_files", f"filtered_playlist_{item_id}.m3u")
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

@router.get("/stream_epg/{item_id}")
async def stream_epg(item_id: int, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    
    epg_path = os.path.join("/app/m3u_files", f"filtered_epg_{item_id}.xml")
    if not os.path.exists(epg_path):
        raise HTTPException(status_code=404, detail="EPG file not found")
    
    # Return the EPG content directly with proper XML headers
    with open(epg_path, "r", encoding="utf-8") as f:
        epg_content = f.read()
    
    return Response(
        content=epg_content,
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="filtered_epg_{item.name}.xml"',
            "Access-Control-Allow-Origin": "*"
        }
    )
