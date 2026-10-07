"""Backup feeds for a live channel: other copies of the same channel on the same provider
account, e.g. "US: FOX NEWS HD" → "VIP: FOX NEWS HD", "AT&T: FOX NEWS ᴿᴬᵂ".

Matching is by channel name with the pack prefix ("US:", "VIP:", …) and quality tags
(HD, UHD, 4K, ᴿᴬᵂ, …) removed. Only packs from the same family are used — the primary's
own pack plus ENGINE_FAILOVER_PREFIXES — so a US channel never fails over to a
foreign-language version. "24/7" loops get no backups (another copy is a different
episode). Backups are always on the primary's own account (same credentials in the
URL), so switching to one never needs an extra provider connection.

The account's full playlist (xtream_playlist_<item>.m3u, ~70MB) is scanned on demand,
once per channel, and the result cached until the playlist changes.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass

import config

logger = logging.getLogger(__name__)

_PREFIX = re.compile(r"^\s*([^:|]{1,12}?)\s*[:|]\s*(.+)$")
_URL = re.compile(r"^(https?://[^/]+/live/[^/]+/[^/]+/)(\d+)(\.\w+)?$")
_DROP_TOKENS = {"HD", "FHD", "UHD", "SD", "4K", "8K", "RAW", "HEVC", "H265", "H264", "60FPS", "50FPS", "HQ"}
_DROP_TRAILING = {"CHANNEL", "NETWORK"}
_NO_BACKUPS = {"24/7"}
_HEAVY = {"4K", "8K", "UHD", "HEVC", "H265"}
_MAX = 5

_lock = threading.Lock()
_cache: dict[tuple, list] = {}


@dataclass(frozen=True)
class Feed:
    url: str
    name: str


def split_name(name: str) -> tuple[str, str]:
    """('US', 'FOX NEWS') from 'US: FOX NEWS HD'; prefix is '' when there is none."""
    m = _PREFIX.match(name)
    prefix, rest = (m.group(1).strip().upper(), m.group(2)) if m else ("", name)
    cleaned = "".join(c if c.isascii() and (c.isalnum() or c in "&+'") else " " for c in rest.upper())
    tokens = [t for t in cleaned.split() if t not in _DROP_TOKENS]
    while len(tokens) > 1 and tokens[-1] in _DROP_TRAILING:
        tokens.pop()
    return prefix, " ".join(tokens)


def match_key(core: str) -> str:
    """Spelling-insensitive key: 'C SPAN 1' and 'CSPAN' both give 'CSPAN'."""
    tokens = core.split()
    if len(tokens) > 1 and tokens[-1] == "1":   # 'C-SPAN 1' is just C-SPAN
        tokens.pop()
    return "".join(c for c in "".join(tokens) if c.isalnum() or c in "+&")   # ESPN+ is not ESPN


def _heavy(name: str) -> bool:
    words = set("".join(c if c.isalnum() else " " for c in name.upper()).split())
    return bool(words & _HEAVY)


def _family(prefix: str) -> list[str]:
    configured = [p.strip().upper() for p in config.ENGINE_FAILOVER_PREFIXES.split(",") if p.strip()]
    if prefix in configured:
        return [prefix] + [p for p in configured if p != prefix]
    return [prefix]


def find(item_id: int, url: str, name: str) -> list[Feed]:
    """Backup feeds for the channel at `url` (best first), excluding the channel itself."""
    m = _URL.match(url)
    if not m or not name:
        return []
    base, stream_id, ext = m.group(1), m.group(2), m.group(3) or ".ts"
    prefix, core = split_name(name)
    if not core or prefix in _NO_BACKUPS:
        return []
    family = _family(prefix)
    path = os.path.join(config.M3U_DIR, f"xtream_playlist_{item_id}.m3u")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    key = (item_id, stream_id, match_key(core), tuple(family), mtime)
    with _lock:
        if key in _cache:
            return _cache[key]
    found = _scan(path, match_key(core), family)
    # Rank: regular feeds before heavy ones (4K/UHD/HEVC); spread across packs, since copies
    # in one pack often share the failing feed's origin; then the family's order.
    ranked = []
    for fam_idx, p in enumerate(family):
        for n, (sid, feed_name) in enumerate(f for f in found.get(p, []) if f[0] != stream_id):
            ranked.append(((_heavy(feed_name), n, fam_idx), sid, feed_name))
    ranked.sort(key=lambda r: r[0])
    feeds = [Feed(f"{base}{sid}{ext}", feed_name) for _, sid, feed_name in ranked[:_MAX]]   # same account
    with _lock:
        _cache[key] = feeds
    return feeds


def _scan(path: str, key: str, family: list[str]) -> dict[str, list[tuple[str, str]]]:
    wanted = set(family)
    # cheap pre-filter: the key's first letters, allowing punctuation/spaces between them
    probe = re.compile(r"[\W_]*".join(map(re.escape, key[:4])), re.IGNORECASE)
    found: dict[str, list[tuple[str, str]]] = {}
    pending: tuple[str, str] | None = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("#EXTINF"):
                pending = None
                if not probe.search(line):
                    continue
                feed_name = line.rsplit(",", 1)[-1].strip()
                prefix, feed_core = split_name(feed_name)
                if prefix in wanted and match_key(feed_core) == key:
                    pending = (prefix, feed_name)
            elif pending and line.strip() and not line.startswith("#"):
                m = _URL.match(line.strip())
                if m:
                    found.setdefault(pending[0], []).append((m.group(2), pending[1]))
                pending = None
    return found
