## Key service files  

**`m3u_service.py`**: `do_fetch_m3u()` downloads full playlist from provider. `build_filter_config()` + `apply_m3u_filter()` apply includes/excludes/language filters. `refresh_filtered_playlist()` re-applies current filters without re-fetching. Background scheduler thread (`start_m3u_scheduler`) checks every 30 min for auto-refresh.

**`epg_manager.py`**: `get_epg()` downloads XMLTV sources, matches channels by normalized name, writes `generated_epg.xml`. Called as a background task after any filtered playlist change.

**`services.py`**: `get_all_item_contexts()` / `get_item_context()` build the template context dicts for all provider pages. `write_count_to_cache()` persists pre-computed counts to sidecar JSON immediately after a file is written — call this at every file write site to avoid re-reading large files on page load.

**`hdhomerun_routes.py`**: `load_channel_lineup()` reads `filtered_playlist_{id}.m3u` and populates the in-memory channel map. `stream_channel()` proxies the IPTV stream with retry/reconnect logic. Active sessions tracked in `_active_streams` dict (module-level, not DB).
