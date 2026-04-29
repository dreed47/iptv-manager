# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Features

A web-based tool for managing, filtering, and serving IPTV playlists and EPG data.  The two core features are:  

* HDHomeRun Tuner emulation - To allow user to expose IPTV channels to media players like Plex, Jellyfin, and Emby.  
* IPTV Provider Proxies - To allow user to define one or more IPTV backend providers and manage playlist and EPG data to then serve to your IPTV Apps. 

## Running the app

```bash
# Docker (normal usage)
cp sample.env .env          # edit HDHR_ADVERTISE_HOST at minimum
docker compose up -d

# Local dev (no Docker)
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 5005 --reload
```

App runs on port 5005 by default. No build step — Python runs directly.

## Architecture

Single-process FastAPI app. Four routers mounted in `main.py`:

| Router | File | Purpose |
|--------|------|---------|
| `router` | `routes.py` | UI pages + management API |
| `hdhomerun_router` | `hdhomerun_routes.py` | HDHomeRun emulation + stream proxy |
| `xtream_server_router` | `xtream_server_routes.py` | Xtream Codes proxy API |
| `health_router` | `health_routes.py` | `/api/health` endpoint |

**Data model** (`models.py`): Single `Item` table (one row per IPTV provider). `AppConfig` table for key/value app settings. SQLite with WAL mode, `data/data.db`.

**File storage** (`m3u_files/` dir, mapped as Docker volume):
- `xtream_playlist_{id}.m3u` — full channel list fetched from provider
- `filtered_playlist_{id}.m3u` — filtered subset served to HDHomeRun/Plex
- `epg_{id}.xml` — raw EPG per provider
- `generated_epg.xml` — merged EPG served at `/epg.xml`
- `counts_{id}.json` / `counts_generated.json` — mtime-keyed sidecar cache (channel counts, avoids reading large M3U/XML on page load)
- `catalog_{id}.db` — SQLite catalog for Xtream VOD/series browsing

**Config** (`config.py`): All env vars read once at import time. Every module imports from here — never call `os.getenv` directly in other files.

## Two separate filter systems — do not confuse

`item.includes` — HDHomeRun channel list, format `100|ESPN,200|FOX`. Used only by `build_filter_config` → `apply_m3u_filter` → `filtered_playlist_{id}.m3u`. Controls what Plex/Jellyfin/Emby sees.

`item.xtream_includes` — Wildcard patterns like `*ESPN*,*Fox News*`. Used only by `xtream_server_routes.py` to filter the Xtream Codes API responses (TiviMate, IPTV Smarters, etc.). Does NOT affect the filtered M3U.

## Key service files

**`m3u_service.py`**: `do_fetch_m3u()` downloads full playlist from provider. `build_filter_config()` + `apply_m3u_filter()` apply includes/excludes/language filters. `refresh_filtered_playlist()` re-applies current filters without re-fetching. Background scheduler thread (`start_m3u_scheduler`) checks every 30 min for auto-refresh.

**`epg_manager.py`**: `get_epg()` downloads XMLTV sources, matches channels by normalized name, writes `generated_epg.xml`. Called as a background task after any filtered playlist change.

**`services.py`**: `get_all_item_contexts()` / `get_item_context()` build the template context dicts for all provider pages. `write_count_to_cache()` persists pre-computed counts to sidecar JSON immediately after a file is written — call this at every file write site to avoid re-reading large files on page load.

**`hdhomerun_routes.py`**: `load_channel_lineup()` reads `filtered_playlist_{id}.m3u` and populates the in-memory channel map. `stream_channel()` proxies the IPTV stream with retry/reconnect logic. Active sessions tracked in `_active_streams` dict (module-level, not DB).

## Async rules

All route handlers are `async def`. Any blocking I/O (file reads, `apply_m3u_filter` on large files, EPG processing) must run in `await asyncio.to_thread(fn)` — direct blocking calls in async handlers stall the entire event loop. Background EPG rebuilds use `BackgroundTasks.add_task(_do_epg_refresh)`.

## Provider URL routing (Xtream proxy)

Each provider gets a URL slug (e.g. `my-provider`). Xtream clients connect at `/{slug}/player_api.php`. The slug routes to the correct `Item` row and uses that item's `proxy_username`/`proxy_password` for auth (not the global `IPTV_USERNAME`/`IPTV_PASSWORD` env vars, which are kept for backwards compat).

## Env vars worth knowing

| Var | Default | Effect |
|-----|---------|--------|
| `HDHR_ADVERTISE_HOST` | `127.0.0.1` | IP Plex uses to reach this app |
| `HDHR_TUNER_COUNT` | `2` | Concurrent tuners advertised to Plex |
| `XTREAM_PREWARM` | `0` | Pre-build VOD/series SQLite catalog on startup (CPU heavy) |
| `LOG_LEVEL` | `INFO` | `DEBUG` for per-request timing |
| `EVENT_LOOP_LAG_MS` | `250` | Logs warning when event loop stalls this long |
| `DUMP_STACKS_ON_LAG` | `0` | Set `1` to faulthandler dump on lag (debug blocking) |
| `EPG_XML_SOURCES` | epg.pw US | Comma-separated XMLTV URLs |
