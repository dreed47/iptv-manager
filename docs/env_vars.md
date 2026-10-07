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

## Streaming Engine

Applies to live channels on HDHomeRun (`/auto/v{n}`) and Xtream (`/live/...`) for non-HLS
sources. One engine per channel: a single provider connection shared by every viewer,
remuxed through FFmpeg (video copied, corrupt packets dropped, audio normalized), and every
viewer starts on a keyframe. VOD/series and `.m3u8` sources always use the legacy proxies.

Provider connections are replaced without viewers noticing: when one closes, errors, goes
silent, runs slower than real time or rewinds, the engine opens a new one and stitches it
on — the output clock carries on where it stopped, the new connection starts on a keyframe,
and content the provider replays on reconnect is trimmed. Viewers get keepalive filler
while that happens, so Plex and IPTV apps keep the stream open.

| Var | Default | Effect |
|-----|---------|--------|
| `STREAM_ENGINE` | `legacy` | `engine` = use the streaming engine; `legacy` = original per-path proxies |
| `ENGINE_AUDIO_CODEC` | `ac3` | Output audio: `ac3` (Plex-safe), `aac`, or `copy` (pass provider audio through) |
| `ENGINE_RING_MB` | `32` | Per-channel buffer that viewers read from |
| `ENGINE_IDLE_SECS` | `15` | Keep a channel's provider connection open this long after the last viewer leaves (fast re-tune). Idle channels are dropped first if a provider's connection limit is reached |
| `ENGINE_START_TIMEOUT` | `20` | Seconds to wait for the first keyframe before answering 503 |
| `ENGINE_STALL_SECS` | `8` | Provider silence that triggers a reconnect |
| `ENGINE_MIN_SPEED` | `0.7` | Reconnect when a provider connection delivers slower than this × real time (`0` = off) |
| `ENGINE_SPEED_WINDOW` | `15` | Seconds that delivery speed is measured over (healthy connections still swing 0.6–1.2× over a few seconds) |
| `ENGINE_SPEED_GRACE` | `4` | Seconds after a (re)connect before speed is judged; doubles after each slow reconnect in a row (max 60) |
| `ENGINE_OUTAGE_SECS` | `45` | Give up and end the stream after this long with nothing to send |
| `ENGINE_KEEPALIVE_SECS` | `2` | Send MPEG-TS null packets to viewers after this long without data (during reconnects) |
| `ENGINE_START_RETRIES` | `3` | Provider connect retries before the first keyframe (channel can't be tuned → 503) |
| `ENGINE_FAILOVER` | `1` | `0` = never switch a channel to a backup copy |
| `ENGINE_FAILOVER_AFTER` | `1` | Failed connections (slow, rewound, stalled, refused, or dead within 60s) within the window that switch to a backup |
| `ENGINE_FAILOVER_WINDOW` | `120` | Seconds those failures are counted over |
| `ENGINE_FAILOVER_COOLDOWN` | `900` | A feed that was switched away from is avoided this long — re-tunes start on a working copy |
| `ENGINE_FAILOVER_PREFIXES` | `US,AT&T,PRIME,TV` | Channel packs that count as the same channel family (all US/English). Packs like VIP (Mexico), GO (Spain), AMP (Caribbean) and GOLD (Europe) carry same-named channels in other languages, so they're left out. A channel in one of these packs can fail over to copies in any of them; a channel in any other pack only to copies in its own pack. Earlier packs are tried first |
| `ENGINE_FAILOVER_EXCLUDE` | _(empty)_ | Comma-separated stream ids or exact channel names never used as backups (e.g. a copy that turned out to be in another language) |

**Backup feeds.** When a channel's feed keeps failing, the engine switches to another copy
of the same channel on the same provider account (so no extra connection is needed) —
e.g. `US: FOX NEWS HD` → `TV: FOX NEWS CHANNEL ᴿᴬᵂ` → `AT&T: FOX NEWS ᴿᴬᵂ`. Names match regardless of spelling (`C-SPAN 1` = `CSPAN`). Copies are matched by
name with the pack prefix and quality tags (HD, 4K, ᴿᴬᵂ, …) removed; 4K/UHD/HEVC copies are
tried last and `24/7:` channels have no backups (another copy is a different episode). The
When a stream tags its audio language, the engine plays the track in the channel's language (English when the channel's own feed has it) and skips a backup whose audio is only in another language — avoided for 24h. Tags aren't always reliable, so `ENGINE_FAILOVER_EXCLUDE` lets you block a specific copy. The switch is stitched like any reconnect, and Active Streams shows `(backup: …)` while a
channel runs on one.

In engine mode a provider's **max sessions** counts open *channels*, not viewers: several
devices watching the same channel use one connection.

---

## Stream Proxy Tuning (legacy)

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
| `STREAM_OBSERVE` | `1` | Analyze live streams (reconnects, timeline replay/reset, keyframe spacing, delivery speed, packet errors) without altering them. View at Tools → Stream Observations or `python3 -m streaming.report` |
| `STREAM_OBSERVE_LOG` | `data/stream_observations.jsonl` | Where per-session observations are appended; empty = log lines only |
