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
STREAM_CHUNK_KB: int             = int(os.getenv("STREAM_CHUNK_KB", "64"))
STREAM_PREBUFFER_KB: int         = int(os.getenv("STREAM_PREBUFFER_KB", "512"))
STREAM_MAX_RETRIES: int          = int(os.getenv("STREAM_MAX_RETRIES", "5"))
STREAM_RETRY_DELAY: float        = float(os.getenv("STREAM_RETRY_DELAY", "3"))
STREAM_READ_TIMEOUT: float       = float(os.getenv("STREAM_READ_TIMEOUT", "30"))
STREAM_SESSION_STALE_SECONDS: int = int(os.getenv("STREAM_SESSION_STALE_SECONDS", "30"))

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
# Feature flags
# ---------------------------------------------------------------------------
ALLOW_FULL_M3U_DOWNLOAD: bool = os.getenv("ALLOW_FULL_M3U_DOWNLOAD", "1").strip() == "1"
HEALTH_CHECK_TVG_ID: str      = os.getenv("HEALTH_CHECK_TVG_ID", "").strip()

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------
M3U_DIR: str = "/app/m3u_files"

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
    if STREAM_MAX_RETRIES < 0:
        errors.append(f"STREAM_MAX_RETRIES must be >= 0 (got {STREAM_MAX_RETRIES})")
    if STREAM_RETRY_DELAY < 0:
        errors.append(f"STREAM_RETRY_DELAY must be >= 0 (got {STREAM_RETRY_DELAY})")
    if STREAM_READ_TIMEOUT < 1:
        errors.append(f"STREAM_READ_TIMEOUT must be >= 1 (got {STREAM_READ_TIMEOUT})")
    if EPG_CACHE_HOURS < 1:
        errors.append(f"EPG_CACHE_HOURS must be >= 1 (got {EPG_CACHE_HOURS})")
    if errors:
        raise ValueError("Invalid configuration:\n" + "\n".join(f"  - {e}" for e in errors))
    logger.debug(
        "Config loaded: base=%s tuners=%d ssdp_off=%s "
        "chunk=%dKB prebuf=%dKB retries=%d epg_cache=%dh",
        ADVERTISED_BASE_URL, HDHR_TUNER_COUNT, HDHR_DISABLE_SSDP,
        STREAM_CHUNK_KB, STREAM_PREBUFFER_KB, STREAM_MAX_RETRIES, EPG_CACHE_HOURS,
    )


_validate()
