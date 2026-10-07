"""
config.py — Application configuration.
All environment variables are read and validated once at import time.
Every other module imports constants from here instead of calling os.getenv.
"""
import os
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HDHomeRun emulation
# ---------------------------------------------------------------------------
HDHR_ADVERTISE_HOST: str  = os.getenv("HDHR_ADVERTISE_HOST") or os.getenv("PUBLIC_HOST") or "127.0.0.1"
HDHR_ADVERTISE_PORT: str  = os.getenv("HDHR_ADVERTISE_PORT") or os.getenv("APP_PORT") or "5005"
HDHR_SCHEME: str          = os.getenv("HDHR_SCHEME") or "http"
HDHR_MODEL: str           = os.getenv("HDHR_MODEL", "HDHR3-US")
HDHR_FRIENDLY_NAME: str   = os.getenv("HDHR_FRIENDLY_NAME", "IPTV HDHomeRun")
HDHR_TUNER_COUNT: int     = int(os.getenv("HDHR_TUNER_COUNT", "2"))
HDHR_DISABLE_SSDP: bool   = os.getenv("HDHR_DISABLE_SSDP", "0") == "1"

# Derived — the public base URL Plex and IPTV clients use to reach this server
ADVERTISED_BASE_URL: str  = f"{HDHR_SCHEME}://{HDHR_ADVERTISE_HOST}:{HDHR_ADVERTISE_PORT}"

# ---------------------------------------------------------------------------
# Stream proxy tuning
# ---------------------------------------------------------------------------
STREAM_CHUNK_KB: int             = int(os.getenv("STREAM_CHUNK_KB", "256"))
HUB_CHUNK_KB:    int             = int(os.getenv("HUB_CHUNK_KB") or os.getenv("STREAM_CHUNK_KB", "256"))  # hub producer chunk size; falls back to STREAM_CHUNK_KB
STREAM_PREBUFFER_KB: int         = int(os.getenv("STREAM_PREBUFFER_KB", "512"))
XTREAM_PREBUFFER_KB: int         = int(os.getenv("XTREAM_PREBUFFER_KB", "0"))
STREAM_MAX_RETRIES: int          = int(os.getenv("STREAM_MAX_RETRIES", "10"))
STREAM_RETRY_DELAY: float        = float(os.getenv("STREAM_RETRY_DELAY", "3"))
STREAM_READ_TIMEOUT: float       = float(os.getenv("STREAM_READ_TIMEOUT", "60"))
STREAM_CONNECT_TIMEOUT: float    = float(os.getenv("STREAM_CONNECT_TIMEOUT", "10"))
STREAM_SESSION_STALE_SECONDS: int = int(os.getenv("STREAM_SESSION_STALE_SECONDS", "30"))

# A connection that delivered at least this many bytes before dropping is considered to
# have been healthy — the retry budget is reset so routine upstream segment closes don't
# exhaust it over a long viewing session. Shared by the HDHomeRun path and the Xtream hub.
STREAM_HEALTHY_SEGMENT_BYTES: int = int(os.getenv("STREAM_HEALTHY_SEGMENT_BYTES", str(10 * 1024 * 1024)))

# Single shared User-Agent for every outbound request this app makes to IPTV providers
# (stream proxies, M3U/EPG fetches, catalog API calls) so it can't silently drift between
# call sites.
PROXY_USER_AGENT: str = os.getenv(
    "PROXY_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
)

# Live streaming engine. "legacy" = original per-path proxies; "engine" = one shared engine
# per channel (FFmpeg-normalized, keyframe-aligned joins). Applies to HDHomeRun /auto/v{n}
# and Xtream /live/... for non-HLS sources; VOD/series are unaffected.
STREAM_ENGINE: str          = os.getenv("STREAM_ENGINE", "legacy").strip().lower()
ENGINE_AUDIO_CODEC: str     = os.getenv("ENGINE_AUDIO_CODEC", "ac3").strip().lower()   # ac3 | aac | copy
ENGINE_RING_MB: int         = int(os.getenv("ENGINE_RING_MB", "32"))
ENGINE_IDLE_SECS: float     = float(os.getenv("ENGINE_IDLE_SECS", "15"))    # keep upstream after last viewer
ENGINE_START_TIMEOUT: float = float(os.getenv("ENGINE_START_TIMEOUT", "20"))  # wait for first keyframe
ENGINE_STALL_SECS: float    = float(os.getenv("ENGINE_STALL_SECS", "15"))   # upstream silence = ended
ENGINE_START_RETRIES: int   = int(os.getenv("ENGINE_START_RETRIES", "3"))   # connect retries before any data

# Observe-only MPEG-TS analysis of live streams (upstream session boundaries, timestamp
# jumps, keyframe spacing, codecs, packet errors). Never alters the bytes. Each finished
# upstream session is appended as one JSON line to STREAM_OBSERVE_LOG ("" = log lines only).
STREAM_OBSERVE: bool    = os.getenv("STREAM_OBSERVE", "1").strip() == "1"
STREAM_OBSERVE_LOG: str = os.getenv(
    "STREAM_OBSERVE_LOG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "stream_observations.jsonl"),
)

# Per-channel shared producer (ChannelHub) — keeps one upstream connection per active channel
# so multiple Apple TV connections share the same stream without opening duplicate upstream TCP connections.
HUB_RING_CHUNKS: int = int(os.getenv("HUB_RING_CHUNKS", "250"))  # ring buffer depth (~6.4 MB at 64 KB chunks)
HUB_IDLE_SECS:   int = int(os.getenv("HUB_IDLE_SECS",   "30"))   # seconds to keep hub alive after last consumer
HUB_CONSUMER_Q:  int = int(os.getenv("HUB_CONSUMER_Q",  "180"))  # per-consumer queue depth (~8 MB at 64 KB chunks)
HUB_SEED_CHUNKS: int = int(os.getenv("HUB_SEED_CHUNKS", "8"))    # ring chunks seeded into new consumer queue (~320 KB)

# HLS master playlist variant selection — if set, the proxy picks the highest-bandwidth
# variant whose bandwidth is <= this threshold (kbps).  0 = disabled (pass URL through unchanged).
HLS_MAX_BANDWIDTH_KBPS: int = int(os.getenv("HLS_MAX_BANDWIDTH_KBPS", "0"))

# ---------------------------------------------------------------------------
# In-browser player (mpegts.js)
# ---------------------------------------------------------------------------
PLAYER_STASH_KB: int      = int(os.getenv("PLAYER_STASH_KB", "1024"))
PLAYER_LATENCY_MAX: float = float(os.getenv("PLAYER_LATENCY_MAX", "30.0"))
PLAYER_LATENCY_MIN: float = float(os.getenv("PLAYER_LATENCY_MIN", "5.0"))

# ---------------------------------------------------------------------------
# Xtream Codes proxy credentials (what downstream IPTV apps connect with)
# ---------------------------------------------------------------------------
IPTV_USERNAME: str = os.getenv("IPTV_USERNAME", "iptv")
IPTV_PASSWORD: str = os.getenv("IPTV_PASSWORD", "iptv")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").strip().upper()

# ---------------------------------------------------------------------------
# Plex integration (optional — enables webhook-driven stream release)
# ---------------------------------------------------------------------------
PLEX_URL: str   = os.getenv("PLEX_URL", "").rstrip("/")
PLEX_TOKEN: str = os.getenv("PLEX_TOKEN", "")

# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------
ALLOW_FULL_M3U_DOWNLOAD: bool = os.getenv("ALLOW_FULL_M3U_DOWNLOAD", "1").strip() == "1"
HEALTH_CHECK_TVG_ID: str      = os.getenv("HEALTH_CHECK_TVG_ID", "").strip()

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------
M3U_DIR: str = os.getenv("M3U_DIR", "/app/m3u_files")

# ---------------------------------------------------------------------------
# EPG
# ---------------------------------------------------------------------------
EPG_CACHE_HOURS: int   = int(os.getenv("EPG_CACHE_HOURS", "12"))
EPG_CACHE_MAX_AGE: int = EPG_CACHE_HOURS * 3600
_epg_raw = os.getenv("EPG_XML_SOURCES", "").strip()
EPG_XML_SOURCES: list[str] = (
    [s.strip() for s in _epg_raw.split(",") if s.strip()]
    if _epg_raw
    else ["https://epg.pw/xmltv/epg_US.xml"]
)

# EPG time offset (hours) — only needed when source sends bare non-UTC times with
# no timezone tag. The _normalize_time_to_utc function handles properly-tagged data.
EPG_TIME_OFFSET_HOURS: int = int(os.getenv("EPG_TIME_OFFSET_HOURS", "0"))

# When False, skip the provider xmltv.php EPG files (epg_{id}.xml).
# Provider EPG is often non-English; US EPG_XML_SOURCES are preferred.
EPG_USE_PROVIDER_DATA: bool = os.getenv("EPG_USE_PROVIDER_DATA", "1").strip() == "1"

# ---------------------------------------------------------------------------
# Startup validation — raises ValueError on obviously bad values so the
# container fails fast with a clear message instead of misbehaving silently.
# ---------------------------------------------------------------------------
def _validate() -> None:
    errors = []
    if HDHR_TUNER_COUNT < 1:
        errors.append(f"HDHR_TUNER_COUNT must be >= 1 (got {HDHR_TUNER_COUNT})")
    if STREAM_CHUNK_KB < 1:
        errors.append(f"STREAM_CHUNK_KB must be >= 1 (got {STREAM_CHUNK_KB})")
    if STREAM_PREBUFFER_KB < 0:
        errors.append(f"STREAM_PREBUFFER_KB must be >= 0 (got {STREAM_PREBUFFER_KB})")
    if XTREAM_PREBUFFER_KB < 0:
        errors.append(f"XTREAM_PREBUFFER_KB must be >= 0 (got {XTREAM_PREBUFFER_KB})")
    if STREAM_MAX_RETRIES < 0:
        errors.append(f"STREAM_MAX_RETRIES must be >= 0 (got {STREAM_MAX_RETRIES})")
    if STREAM_RETRY_DELAY < 0:
        errors.append(f"STREAM_RETRY_DELAY must be >= 0 (got {STREAM_RETRY_DELAY})")
    if STREAM_READ_TIMEOUT < 1:
        errors.append(f"STREAM_READ_TIMEOUT must be >= 1 (got {STREAM_READ_TIMEOUT})")
    if STREAM_CONNECT_TIMEOUT < 1:
        errors.append(f"STREAM_CONNECT_TIMEOUT must be >= 1 (got {STREAM_CONNECT_TIMEOUT})")
    if STREAM_HEALTHY_SEGMENT_BYTES < 1:
        errors.append(f"STREAM_HEALTHY_SEGMENT_BYTES must be >= 1 (got {STREAM_HEALTHY_SEGMENT_BYTES})")
    if EPG_CACHE_HOURS < 1:
        errors.append(f"EPG_CACHE_HOURS must be >= 1 (got {EPG_CACHE_HOURS})")
    if HUB_SEED_CHUNKS < 1:
        errors.append(f"HUB_SEED_CHUNKS must be >= 1 (got {HUB_SEED_CHUNKS})")
    if HUB_CHUNK_KB < 1:
        errors.append(f"HUB_CHUNK_KB must be >= 1 (got {HUB_CHUNK_KB})")
    if STREAM_ENGINE not in ("legacy", "engine"):
        errors.append(f"STREAM_ENGINE must be 'legacy' or 'engine' (got {STREAM_ENGINE!r})")
    if ENGINE_AUDIO_CODEC not in ("ac3", "aac", "copy"):
        errors.append(f"ENGINE_AUDIO_CODEC must be ac3, aac or copy (got {ENGINE_AUDIO_CODEC!r})")
    if errors:
        raise ValueError("Invalid configuration:\n" + "\n".join(f"  - {e}" for e in errors))
    logger.debug(
        "Config loaded: base=%s tuners=%d ssdp_off=%s "
        "chunk=%dKB prebuf=%dKB retries=%d epg_cache=%dh",
        ADVERTISED_BASE_URL, HDHR_TUNER_COUNT, HDHR_DISABLE_SSDP,
        STREAM_CHUNK_KB, STREAM_PREBUFFER_KB, STREAM_MAX_RETRIES, EPG_CACHE_HOURS,
    )


_validate()
