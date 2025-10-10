# routes.py
from fastapi import APIRouter, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from models import get_db, Item
from schemas import ItemCreate, ItemUpdate, ItemResponse
from services import create_item, update_item, delete_item, get_all_items
from starlette.requests import Request
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

@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db), error: str = None, success: str = None):
    items = get_all_items(db)
    items_with_files = []
    for item in items:
        file_path = os.path.join("/app/m3u_files", f"xtream_playlist_{item.id}.m3u")
        item_dict = item.__dict__
        item_dict['has_m3u'] = os.path.exists(file_path)
        items_with_files.append(item_dict)
    return templates.TemplateResponse("index.html", {"request": request, "items": items_with_files, "error": error, "success": success})

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
    item_id: int = Form(None),
    new_name: str = Form(None),
    new_server_url: str = Form(None),
    new_username: str = Form(None),
    new_user_pass: str = Form(None),
    db: Session = Depends(get_db)
):
    logger.info(f"Received form data: add={add}, edit={edit}, delete={delete}, name='{name}', server_url='{server_url}', username='{username}', user_pass='{user_pass}', item_id={item_id}, new_name='{new_name}', new_server_url='{new_server_url}', new_username='{new_username}', new_user_pass='{new_user_pass}'")
    if add:
        logger.info(f"Processing add request with name: '{name}'")
        result = create_item(db, name, server_url, username, user_pass)
        if not result:
            logger.warning("Item creation failed")
            return RedirectResponse(url="/?error=Failed to create item", status_code=303)
    elif edit or (item_id and new_name and new_server_url and new_username and new_user_pass and not add and not delete):
        logger.info(f"Processing edit request for item {item_id}")
        if not item_id or not all([new_name, new_server_url, new_username, new_user_pass]):
            logger.warning(f"Missing item_id or fields for edit: item_id={item_id}")
            return RedirectResponse(url="/?error=Missing item ID or fields", status_code=303)
        if not update_item(db, item_id, new_name, new_server_url, new_username, new_user_pass):
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
        
        # Headers for all requests
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": item.server_url.rstrip('/'),
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive"
        }
        
        # Try Xtream Codes API first
        base_url = f"{item.server_url.rstrip('/')}/player_api.php"
        auth_url = f"{base_url}?username={urllib.parse.quote(item.username)}&password={urllib.parse.quote(item.user_pass)}"
        logger.info(f"Attempting Xtream API auth: {auth_url}")
        
        m3u_content = None
        num_records = 0
        source = "Xtream API"
        try:
            # Authenticate
            response = requests.get(auth_url, headers=headers, timeout=30)
            response.raise_for_status()
            user_data = response.json()
            logger.info(f"Xtream API auth response: {json.dumps(user_data, indent=2)[:500]}")
            
            if user_data.get('user_info', {}).get('auth', 0) != 1:
                logger.warning(f"Invalid Xtream Codes credentials for item {item_id}")
                raise ValueError("Invalid credentials")
            
            logger.info(f"Authenticated with Xtream Codes for user {item.username}")
            
            # Fetch live streams
            live_streams_url = f"{auth_url}&action=get_live_streams"
            live_streams_response = requests.get(live_streams_url, headers=headers, timeout=30)
            live_streams_response.raise_for_status()
            live_streams = live_streams_response.json()
            
            # Fetch VOD streams
            vod_streams_url = f"{auth_url}&action=get_vod_streams"
            vod_streams_response = requests.get(vod_streams_url, headers=headers, timeout=30)
            vod_streams_response.raise_for_status()
            vod_streams = vod_streams_response.json()
            
            # Fetch series
            series_url = f"{auth_url}&action=get_series"
            series_response = requests.get(series_url, headers=headers, timeout=30)
            series_response.raise_for_status()
            series = series_response.json()
            
            num_records = len(live_streams) + len(vod_streams) + len(series)
            logger.info(f"Fetched {len(live_streams)} live streams, {len(vod_streams)} VOD streams, {len(series)} series (total: {num_records})")
            
            # Generate M3U content
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
            
            # Fallback to original M3U URL
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
            
            # Count actual stream entries (lines following #EXTINF)
            num_records = len([line for line in m3u_content.splitlines() if line.startswith("#EXTINF")])
        
        # Save M3U file
        output_dir = "/app/m3u_files"
        os.makedirs(output_dir, exist_ok=True)
        m3u_file_path = os.path.join(output_dir, f"xtream_playlist_{item_id}.m3u")
        with open(m3u_file_path, "w", encoding="utf-8") as f:
            f.write(m3u_content)
        
        total_lines = len(m3u_content.splitlines())
        logger.info(f"Generated and saved {source} playlist for item {item_id} ({num_records} records, {total_lines} lines) at {m3u_file_path}")
        
        # Fetch EPG
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
        
        # Construct redirect URL with success message
        redirect_url = f"/?success=Saved {num_records} records ({total_lines} lines) to M3U file from {source}"
        if epg_error:
            redirect_url += f"&error={urllib.parse.quote(epg_error)}"
        
        return RedirectResponse(url=redirect_url, status_code=303)
    
    except Exception as e:
        logger.error(f"Failed to generate M3U for item {item_id}: {str(e)}")
        return RedirectResponse(url=f"/?error=Failed to save M3U file: {str(e)}", status_code=303)

@router.get("/download_m3u/{item_id}", response_class=FileResponse)
async def download_m3u(item_id: int, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    
    file_path = os.path.join("/app/m3u_files", f"xtream_playlist_{item_id}.m3u")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="M3U file not found")
    
    return FileResponse(file_path, filename=f"xtream_playlist_{item.name}.m3u")
