## Env vars worth knowing

| Var | Default | Effect |
|-----|---------|--------|
| `HDHR_ADVERTISE_HOST` | `127.0.0.1` | IP Plex uses to reach this app |
| `HDHR_TUNER_COUNT` | `2` | Concurrent tuners advertised to Plex |
| `XTREAM_PREWARM` | `0` | Pre-build VOD/series SQLite catalog on startup (CPU heavy) |
| `LOG_LEVEL` | `INFO` | `DEBUG` for per-request timing |
| `EVENT_LOOP_LAG_MS` | `250` | Logs warning when event loop stalls this long |
| `DUMP_STACKS_ON_LAG` | `0` | Set `1` to faulthandler dump on lag (debug blocking) |
| `EPG_XML_SOURCES` | epg.pw US | Comma-separated XMLTV URLs to use as EPG source |
| `EPG_CACHE_HOURS` | `12` | How long to cache the generated EPG before rebuilding |
| `EPG_TIME_OFFSET_HOURS` | `0` | Shift all EPG times by N hours (use when source timestamps are wrong) |
| `EPG_USE_PROVIDER_DATA` | `1` | Also use the provider's own xmltv.php EPG as a fallback source |
