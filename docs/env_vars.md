# Environment Variables

All variables are optional unless marked **required**. Defaults shown.

---

## Docker / Deployment

| Var | Default | Effect |
|-----|---------|--------|
| `APP_PORT` | `5005` | Host port (bridge mode) and uvicorn listen port |
| `CONTAINER_NAME` | `iptv-app` | Docker container name — change when running multiple instances |
| `MEMORY_LIMIT` | `512m` | Docker memory cap (`256m`, `512m`, `1g`, …) |
| `CPU_LIMIT` | `1.0` | Docker CPU cores cap |
| `UVICORN_MAX_REQUESTS` | `100000` | Worker restarts after this many requests (memory-leak safety valve) |
| `RESTART_POLICY` | `unless-stopped` | Docker restart policy (`no`, `always`, `unless-stopped`, `on-failure`) |
| `TZ` | `UTC` | Timezone for log timestamps (tz database name, e.g. `America/New_York`) |

---

## HDHomeRun Emulation

| Var | Default | Effect |
|-----|---------|--------|
| `HDHR_ADVERTISE_HOST` | `127.0.0.1` | IP that Plex uses to reach this app — set to LAN IP if Plex is on another machine |
| `HDHR_ADVERTISE_PORT` | `APP_PORT` or `5005` | Port advertised to Plex; override only when it differs from `APP_PORT` |
| `HDHR_SCHEME` | `http` | `http` or `https` — scheme included in the advertised base URL |
| `HDHR_MODEL` | `HDHR3-US` | Model number shown in Plex (`HDHR3-US`, `HDHR4-2US`, `HDHR5-4US`, …) |
| `HDHR_FRIENDLY_NAME` | `IPTV HDHomeRun` | Friendly name shown in Plex — useful when running multiple instances |
| `HDHR_TUNER_COUNT` | `2` | Concurrent tuners advertised to Plex |
| `HDHR_DISABLE_SSDP` | `0` | `1` = disable SSDP auto-discovery (works everywhere); `0` = enable (Linux only) |

---

## Xtream Codes Server

| Var | Default | Effect |
|-----|---------|--------|
| `IPTV_USERNAME` | `iptv` | Username IPTV apps (TiviMate, Smarters, VLC) use to connect **to this server** |
| `IPTV_PASSWORD` | `iptv` | Password for the above — not your upstream provider credentials |
| `XTREAM_PREWARM` | `0` | `1` = pre-build VOD/series SQLite catalog on startup (CPU heavy) |
| `XTREAM_PREWARM_DELAY_S` | `15` | Seconds after startup before prewarm begins |

---

## Stream Proxy Tuning

| Var | Default | Effect |
|-----|---------|--------|
| `STREAM_CHUNK_KB` | `256` | Chunk size for the in-browser player path |
| `HUB_CHUNK_KB` | `STREAM_CHUNK_KB` | Chunk size for the per-channel hub producer — keep small (64–256) for live streams |
| `STREAM_PREBUFFER_KB` | `512` | Server-side prebuffer before sending to browser; `0` to disable |
| `XTREAM_PREBUFFER_KB` | `0` | Server-side prebuffer for Xtream/TiviMate live streams; `0` to disable |
| `STREAM_MAX_RETRIES` | `10` | Reconnect attempts when upstream stream drops; `0` to disable |
| `STREAM_RETRY_DELAY` | `3` | Seconds between reconnect attempts |
| `STREAM_READ_TIMEOUT` | `60` | Seconds without data before proxy treats stream as stale and reconnects |
| `STREAM_SESSION_STALE_SECONDS` | `30` | Seconds after last chunk before a session is considered dead |
| `HLS_MAX_BANDWIDTH_KBPS` | `0` | Auto-select lower-bitrate HLS variant at this cap (kbps); `0` = disabled |

---

## ChannelHub Ring Buffer

Shared per-channel upstream producer — multiple clients share one upstream TCP connection.

| Var | Default | Effect |
|-----|---------|--------|
| `HUB_RING_CHUNKS` | `250` | Ring buffer depth (~16 MB at 64 KB chunks) |
| `HUB_IDLE_SECS` | `30` | Seconds to keep hub alive after last consumer disconnects |
| `HUB_CONSUMER_Q` | `180` | Per-consumer queue depth (~8 MB at 64 KB chunks) |
| `HUB_SEED_CHUNKS` | `8` | Ring chunks pre-seeded into a new consumer's queue (~512 KB) |

---

## In-Browser Player (mpegts.js)

| Var | Default | Effect |
|-----|---------|--------|
| `PLAYER_STASH_KB` | `1024` | Client-side stash buffer in KB — larger absorbs more jitter at cost of startup latency |
| `PLAYER_LATENCY_MAX` | `30` | Max live buffer latency in seconds before mpegts.js skips ahead |
| `PLAYER_LATENCY_MIN` | `5` | Minimum buffer to keep in seconds |

---

## EPG

| Var | Default | Effect |
|-----|---------|--------|
| `EPG_XML_SOURCES` | `epg.pw US` | Comma-separated XMLTV URLs — leave unset to use epg.pw default |
| `EPG_CACHE_HOURS` | `12` | Hours to cache the generated EPG before rebuilding |
| `EPG_TIME_OFFSET_HOURS` | `0` | Shift all EPG times by N hours — only needed when source sends bare non-UTC times with no timezone tag |
| `EPG_USE_PROVIDER_DATA` | `1` | `0` = skip provider xmltv.php EPG files (useful when provider EPG is non-English or low quality) |
| `EPG_REFRESH_COOLDOWN_S` | `60` | Minimum seconds between EPG rebuild triggers |

---

## Plex Integration

Optional. When set, enables webhook-driven stream release so Plex can signal the proxy to tear down idle streams.

| Var | Default | Effect |
|-----|---------|--------|
| `PLEX_URL` | _(disabled)_ | Plex server base URL, e.g. `http://192.168.1.10:32400` |
| `PLEX_TOKEN` | _(disabled)_ | Plex auth token (find at `plex.tv/devices.xml` or in Plex server logs) |

---

## Feature Flags

| Var | Default | Effect |
|-----|---------|--------|
| `ALLOW_FULL_M3U_DOWNLOAD` | `1` | `0` = disable the Fetch M3U button in UI (prevents accidental re-fetches or provider rate-limit hits) |
| `HEALTH_CHECK_TVG_ID` | _(disabled)_ | Channel TVG-ID used by `GET /api/health` for automated monitoring (Uptime Kuma, Home Assistant) |

---

## Logging & Diagnostics

| Var | Default | Effect |
|-----|---------|--------|
| `LOG_LEVEL` | `INFO` | `DEBUG` for per-request timing; `WARNING` for errors only |
| `EVENT_LOOP_LAG_MS` | `250` | Logs a warning when the async event loop stalls this long |
| `DUMP_STACKS_ON_LAG` | `0` | `1` = faulthandler stack dump on event loop lag (debug blocking coroutines) |
| `DUMP_STACKS_MIN_INTERVAL_S` | `30` | Minimum seconds between stack dumps when `DUMP_STACKS_ON_LAG=1` |
| `SLOW_REQUEST_MS` | `2000` | Logs a warning for requests that take longer than this (ms) |
