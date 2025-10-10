from fastapi import APIRouter, Depends, HTTPException, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from models import get_db, Item
from schemas import ItemCreate, ItemUpdate, ItemResponse
from services import create_item, update_item, delete_item, get_all_items
import logging
import os
import urllib.parse
import requests
import json
import re

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

def get_base_url(request: Request) -> str:
    """Get the base URL including protocol and host"""
    base_url = str(request.base_url)
    # Remove trailing slash if present
    return base_url.rstrip('/')

@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db), error: str = None, success: str = None):
    items = get_all_items(db)
    items_with_files = []
    base_url = get_base_url(request)
    
    for item in items:
        m3u_path = os.path.join("/app/m3u_files", f"xtream_playlist_{item.id}.m3u")
        filtered_path = os.path.join("/app/m3u_files", f"filtered_playlist_{item.id}.m3u")
        item_dict = item.__dict__
        item_dict['has_m3u'] = os.path.exists(m3u_path)
        item_dict['has_filtered'] = os.path.exists(filtered_path)
        # Add streaming URL to the item
        item_dict['stream_url'] = f"{base_url}/stream_filtered_m3u/{item.id}"
        items_with_files.append(item_dict)
    
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "items": items_with_files, 
        "error": error, 
        "success": success,
        "base_url": base_url
    })

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
    db: Session = Depends(get_db)
):
    logger.info(f"Received form data: add={add}, edit={edit}, delete={delete}, name='{name}', server_url='{server_url}', username='{username}', user_pass='{user_pass}', languages='{languages}', includes='{includes}', excludes='{excludes}', item_id={item_id}, new_name='{new_name}', new_server_url='{new_server_url}', new_username='{new_username}', new_user_pass='{new_user_pass}', new_languages='{new_languages}', new_includes='{new_includes}', new_excludes='{new_excludes}'")
    
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
    
    if add:
        logger.info(f"Processing add request with name: '{name}'")
        result = create_item(db, name, server_url, username, user_pass, languages, includes, excludes)
        if not result:
            logger.warning("Item creation failed")
            return RedirectResponse(url="/?error=Failed to create item", status_code=303)
    elif edit or (item_id and new_name and new_server_url and new_username and new_user_pass and not add and not delete):
        logger.info(f"Processing edit request for item {item_id}")
        if not item_id or not all([new_name, new_server_url, new_username, new_user_pass]):
            logger.warning(f"Missing item_id or fields for edit: item_id={item_id}")
            return RedirectResponse(url="/?error=Missing item ID or fields", status_code=303)
        # FIXED: Added new_includes parameter
        if not update_item(db, item_id, new_name, new_server_url, new_username, new_user_pass, new_languages, new_includes, new_excludes):
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
        
        # Read original M3U
        with open(m3u_path, "r", encoding="utf-8") as f:
            m3u_content = f.read()
        
        # Parse languages, includes, and excludes
        languages = [lang.strip().lower() for lang in (item.languages or "").split(",") if lang.strip()]
        includes = [inc.strip().lower() for inc in (item.includes or "").split(",") if inc.strip()]
        excludes = [ex.strip().lower() for ex in (item.excludes or "").split(",") if ex.strip()]
        logger.info(f"Filtering item {item_id} with languages={languages}, includes={includes}, excludes={excludes}")
        
        # Filter M3U
        filtered_content = "#EXTM3U\n"
        lines = m3u_content.splitlines()
        num_records = 0
        unmatched_count = 0
        i = 0
        
        # Skip the original #EXTM3U line if it exists
        if lines and lines[0].strip() == "#EXTM3U":
            i = 1
        
        while i < len(lines):
            if lines[i].startswith("#EXTINF"):
                if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                    extinf = lines[i]
                    url = lines[i + 1]
                    
                    include = True
                    
                    # Parse EXTINF attributes
                    attributes = {}
                    channel_name = ""
                    
                    # Extract attributes and channel name
                    if " " in extinf and "," in extinf:
                        # Format: #EXTINF:-1 attr1="value1" attr2="value2",Channel Name
                        attr_part, channel_name = extinf.split(",", 1)
                        # Parse attributes
                        attr_matches = re.findall(r'(\S+?)="([^"]*)"', attr_part)
                        for key, value in attr_matches:
                            attributes[key.lower()] = value.lower()
                    else:
                        # Simple format: #EXTINF:-1,Channel Name
                        channel_name = extinf.split(",", 1)[1] if "," in extinf else ""
                    
                    # Extract language from tvg-name (first 2 characters before " - ")
                    tvg_name = attributes.get('tvg-name', '')
                    channel_language = ""
                    
                    if " - " in tvg_name:
                        # Format: "FR - NCIS: Origins (2024) (US)"
                        channel_language = tvg_name.split(" - ")[0].strip().lower()
                    elif len(tvg_name) >= 2:
                        # Fallback: just take first 2 characters
                        channel_language = tvg_name[:2].lower()
                    
                    # Build search text for includes/excludes
                    search_text = f"{tvg_name} {channel_name.lower()}".lower()
                    
                    # Apply language filter if languages are specified
                    if languages:
                        include = False
                        for lang in languages:
                            # Check if the extracted language matches
                            if lang.lower() == channel_language:
                                include = True
                                logger.debug(f"Language match: '{lang}' == '{channel_language}' in: {tvg_name}")
                                break
                    
                    # Apply excludes and includes logic (includes override excludes)
                    if include:
                        # First check if it should be excluded
                        excluded = False
                        if excludes:
                            for ex in excludes:
                                if ex in search_text:
                                    excluded = True
                                    logger.debug(f"Exclusion match: '{ex}' found in: {tvg_name}")
                                    break
                        
                        # Then check if it should be included (overrides exclusion)
                        included_override = False
                        if includes:
                            for inc in includes:
                                if inc in search_text:
                                    included_override = True
                                    logger.debug(f"Inclusion override: '{inc}' found in: {tvg_name}")
                                    break
                        
                        # Final decision: if excluded but also included, include it
                        if excluded and not included_override:
                            include = False
                        elif excluded and included_override:
                            include = True
                            logger.debug(f"Inclusion overrides exclusion for: {tvg_name}")
                    
                    if include:
                        filtered_content += f"{extinf}\n{url}\n"
                        num_records += 1
                    else:
                        unmatched_count += 1
                        logger.debug(f"Excluded channel: {tvg_name}")
                i += 2
            else:
                # Copy other non-EXTINF lines but skip duplicate #EXTM3U
                if lines[i].startswith("#") and not lines[i].startswith("#EXTINF") and lines[i].strip() != "#EXTM3U":
                    filtered_content += f"{lines[i]}\n"
                i += 1
        
        logger.info(f"Filtering results: {num_records} included, {unmatched_count} excluded")
        
        if num_records == 0:
            logger.warning(f"No records matched filter for item {item_id}: languages={item.languages}, includes={item.includes}, excludes={item.excludes}")
            return RedirectResponse(url=f"/?error=No records matched the filter criteria. Language codes not found in channel data.", status_code=303)
        
        # Save filtered M3U
        output_dir = "/app/m3u_files"
        os.makedirs(output_dir, exist_ok=True)
        filtered_file_path = os.path.join(output_dir, f"filtered_playlist_{item_id}.m3u")
        with open(filtered_file_path, "w", encoding="utf-8") as f:
            f.write(filtered_content)
        
        total_lines = len(filtered_content.splitlines())
        logger.info(f"Generated and saved filtered playlist for item {item_id} ({num_records} records, {total_lines} lines) at {filtered_file_path}")
        
        return RedirectResponse(url=f"/?success=Saved {num_records} filtered records ({total_lines} lines) to filtered M3U file", status_code=303)
    
    except Exception as e:
        logger.error(f"Failed to generate filtered M3U for item {item_id}: {str(e)}")
        return RedirectResponse(url=f"/?error=Failed to save filtered M3U file: {str(e)}", status_code=303)

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
