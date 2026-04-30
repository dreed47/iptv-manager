## Architecture


Single-process FastAPI app. Four routers mounted in `main.py`:

| Router | File | Purpose |
|--------|------|---------|
| `router` | `routes.py` | UI pages + management API |
| `hdhomerun_router` | `hdhomerun_routes.py` | HDHomeRun emulation + stream proxy |
| `xtream_server_router` | `xtream_server_routes.py` | Xtream Codes proxy API |
| `health_router` | `health_routes.py` | `/api/health` endpoint |

**Data model** (`models.py`): Three tables. `Item` — one row per IPTV provider. `AppConfig` — key/value app settings. `EpgChannelMap` — explicit `tvg_id → epg_source_id` overrides for the EPG builder. SQLite with WAL mode, `data/data.db`.

**File storage** (`m3u_files/` dir, mapped as Docker volume):
- `xtream_playlist_{id}.m3u` — full channel list fetched from provider
- `filtered_playlist_{id}.m3u` — filtered subset served to HDHomeRun/Plex
- `epg_{id}.xml` — raw EPG per provider
- `generated_epg.xml` — merged EPG served at `/epg.xml`
- `counts_{id}.json` / `counts_generated.json` — mtime-keyed sidecar cache (channel counts, avoids reading large M3U/XML on page load)
- `catalog_{id}.db` — SQLite catalog for Xtream VOD/series browsing

**Config** (`config.py`): All env vars read once at import time. Every module imports from here — never call `os.getenv` directly in other files.
