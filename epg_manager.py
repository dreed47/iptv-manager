"""
EPG Manager
-----------
Builds a custom XMLTV file by:
  1. Reading all filtered_playlist_*.m3u files to get our channel list
     (tvg-id, tvg-chno, clean name, logo)
  2. Fetching XMLTV data from one or more source URLs (iptv-org US guides)
  3. Matching our channels to source channels by normalized display name
  4. Outputting an XMLTV where channel IDs == our tvg-id values, so Plex
     can auto-match via GuideSourceID without any manual channel mapping

The result is cached at EPG_CACHE_PATH and regenerated when the cache is
older than EPG_CACHE_MAX_AGE seconds (default 12 hours) or when a manual
refresh is triggered via POST /epg/refresh.

Configuration via environment variables:
  EPG_XML_SOURCES  Comma-separated list of XMLTV URLs to fetch.
                   Defaults to two iptv-org US guides (DirecTV + TVGuide).
  EPG_CACHE_HOURS  Cache lifetime in hours. Default: 12
"""

import os
import re
import tempfile
import time
import logging
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import config
import requests
from services import write_count_to_cache

logger = logging.getLogger(__name__)

EPG_CACHE_PATH = config.M3U_DIR + "/generated_epg.xml"
EPG_CACHE_MAX_AGE = config.EPG_CACHE_MAX_AGE


# ---------------------------------------------------------------------------
# Name normalisation helpers
# ---------------------------------------------------------------------------

_SUPERSCRIPT_RE = re.compile(r'[\u1D00-\u1DBF\u02B0-\u02FF\u00B2\u00B3\u00B9\u2070-\u209F\u1D2C-\u1D9A]+')
_PROVIDER_PREFIX_RE = re.compile(r'^[A-Z]+:\s*')
_LOOP_PREFIX_RE = re.compile(r'^\d+/\d+:\s*')   # e.g. "24/7:" — loop/replay channels
_HD_SUFFIX_RE = re.compile(r'\b(hd|sd|4k)\b', re.I)
_NONALNUM_RE = re.compile(r'[^a-z0-9\s]')
_PAREN_RE = re.compile(r'\([^)]*\)')         # strip e.g. "(NBC)", "(KDKA)"
_DIGIT_SPACE_RE = re.compile(r'(\d)\s+(\d)')  # collapse "2 HD" digit gaps

# Canonical alias map: our normalized name -> list of alternate normalized names to try.
# Handles abbreviations vs full names and common branding differences.
_ALIASES: dict[str, list[str]] = {
    "espn 2":                      ["espn2", "espn2 hd", "espn 2 hd", "espn2 hd"],
    "espn":                        ["espn hd"],
    "fox news":                    ["fox news channel", "fox news hd", "fox news channel hd"],
    "fox business network":        ["fox business", "fox business hd", "fox business network hd"],
    "nat geo wild":                ["national geographic wild", "natgeo wild", "nat geo wild hd", "ng wild"],
    "national geographic channel": ["national geographic", "nat geo", "natgeo", "national geographic hd"],
    "tcm":                         ["turner classic movies", "turner classic movies hd", "tcm hd"],
    "travel channel":              ["travel channel hd", "the travel channel", "the travel channel hd"],
    "diy":                         ["diy network", "diy network hd", "magnolia network", "magnolia"],
    "smithsonian channel":         ["smithsonian", "smithsonian hd", "smithsonian channel hd"],
    "sundance tv":                 ["sundancetv", "sundance channel", "sundance", "sundance tv hd"],
    "fanduel tv":                  ["bally sports", "fanduel sports network", "fanduel", "fanduel sports"],
    "mlb network":                 ["mlb network hd", "mlb"],
    "amc":                         ["amc hd"],
    "tnt":                         ["tnt hd"],
    "fx":                          ["fx hd"],
    "bravo":                       ["bravo hd"],
    "wtae pittsburgh":             ["wtae", "wtae abc"],
    "kdka pittsburgh":             ["kdka", "kdka cbs", "cbs 2"],
    "wpxi pittsburgh":             ["wpxi", "wpxi nbc"],
}


def _strip_prefix(name: str) -> str:
    """Remove SLING:, US:, GO:, PRIME:, 24/7: etc. and trailing superscripts."""
    name = _PROVIDER_PREFIX_RE.sub('', name).strip()
    name = _LOOP_PREFIX_RE.sub('', name).strip()
    name = _SUPERSCRIPT_RE.sub(' ', name).strip()
    return name


def _normalize(s: str) -> str:
    """Lowercase, strip accents, parentheticals, HD/SD suffix, punctuation, extra spaces."""
    s = (s or "").lower()
    s = _PAREN_RE.sub(' ', s)           # remove (NBC), (KDKA), etc.
    s = _SUPERSCRIPT_RE.sub(' ', s)
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = _HD_SUFFIX_RE.sub('', s)
    s = _NONALNUM_RE.sub(' ', s)
    return ' '.join(s.split())


# ---------------------------------------------------------------------------
# Read our channel list from filtered M3U files
# ---------------------------------------------------------------------------

def get_channels_from_m3u(item_ids: list[int] | None = None) -> list[dict]:
    """
    Parse hdhr_filtered_playlist.m3u and return a de-duplicated list
    of channel dicts: {tvg_id, tvg_chno, raw_name, clean_name, norm, logo}
    item_ids is accepted for API compatibility but ignored (channels come from
    the global HDHR playlist, not per-provider filtered playlists).
    """
    channels: list[dict] = []
    seen_ids: set[str] = set()
    m3u_dir = config.M3U_DIR

    from m3u_service import HDHR_PLAYLIST_FILENAME
    hdhr_files = [HDHR_PLAYLIST_FILENAME]

    for filename in hdhr_files:
        filepath = os.path.join(m3u_dir, filename)
        if not os.path.exists(filepath):
            continue
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as exc:
            logger.warning(f"Could not read {filepath}: {exc}")
            continue

        for i, line in enumerate(lines):
            line = line.strip()
            if not line.startswith('#EXTINF') or i + 1 >= len(lines):
                continue

            # Skip VOD and series — only live streams have EPG data
            url_line = lines[i + 1].strip()
            if '/movie/' in url_line or '/series/' in url_line:
                continue

            tvg_id_m = re.search(r'tvg-id="([^"]+)"', line)
            tvg_name_m = re.search(r'tvg-name="([^"]+)"', line)
            tvg_chno_m = re.search(r'tvg-chno="([^"]+)"', line)
            logo_m = re.search(r'tvg-logo="([^"]+)"', line)

            if not tvg_id_m or not tvg_name_m:
                continue
            tvg_id = tvg_id_m.group(1)
            if tvg_id in seen_ids:
                continue
            seen_ids.add(tvg_id)

            raw_name = tvg_name_m.group(1)
            clean = _strip_prefix(raw_name)
            # Channels with a "24/7:" style prefix are loop/replay streams — their
            # programme schedule won't match broadcast EPG data, so skip matching.
            is_loop = bool(_LOOP_PREFIX_RE.match(raw_name))
            channels.append({
                'tvg_id': tvg_id,
                'tvg_chno': tvg_chno_m.group(1) if tvg_chno_m else '',
                'raw_name': raw_name,
                'clean_name': clean,
                'norm': _normalize(clean),
                'logo': logo_m.group(1) if logo_m else '',
                'epg_skip': is_loop,
            })

    logger.info(f"EPG: found {len(channels)} unique channels in filtered M3U files")
    return channels


# ---------------------------------------------------------------------------
# Fetch source XMLTV
# ---------------------------------------------------------------------------

def _fetch_epg_source(url: str):
    """
    Fetch and parse an XMLTV URL.
    Streams the download to a temp file to avoid holding both the raw bytes and
    the parsed XML tree in RAM simultaneously (large feeds can be 100MB+).
    Returns (channel_elements, programme_elements) or (None, None) on failure.
    """
    tmp_path = None
    try:
        logger.debug(f"EPG: fetching {url}")
        resp = requests.get(url, timeout=90, stream=True, headers={'Accept-Encoding': 'gzip, deflate'})
        resp.raise_for_status()
        with tempfile.NamedTemporaryFile(delete=False, suffix='.xml') as tmp:
            tmp_path = tmp.name
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    tmp.write(chunk)
        root = ET.parse(tmp_path).getroot()
        channels = root.findall('channel')
        programmes = root.findall('programme')
        logger.debug(f"EPG:   got {len(channels)} channels, {len(programmes)} programmes from {url}")
        return channels, programmes
    except Exception as exc:
        logger.warning(f"EPG: failed to fetch {url}: {exc}")
        return None, None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _load_epg_from_file(path: str):
    """Parse a local XMLTV file. Returns (channel_elements, programme_elements) or (None, None)."""
    try:
        root = ET.parse(path).getroot()
        channels = root.findall('channel')
        programmes = root.findall('programme')
        logger.debug(f"EPG: loaded {len(channels)} channels, {len(programmes)} programmes from {path}")
        return channels, programmes
    except Exception as exc:
        logger.warning(f"EPG: failed to parse {path}: {exc}")
        return None, None


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

_FILLER = frozenset({'channel', 'network', 'the', 'hd', 'sd', '4k'})


def _find_match(src_norm: str, our_by_norm: dict) -> dict | None:
    """
    Try to match a source channel's normalized name to one of our channels.
    Strategy (in order):
      1. Exact normalized match
      2. Alias lookup (src_norm matches a known alias for one of our channels)
      3. Token-set equality after stripping filler words
      4. One-word core subset — word must be >= 4 chars to avoid "one"/"two" etc.
      5. Filler-word diff only ("Fox News" vs "Fox News Channel")
    """
    # 1. Exact
    ch = our_by_norm.get(src_norm)
    if ch:
        return ch

    src_tokens = set(src_norm.split())
    if not src_tokens:
        return None

    # 2. Alias lookup
    for our_norm, ch in our_by_norm.items():
        if src_norm in _ALIASES.get(our_norm, ()):
            return ch

    # Pre-compute src tokens without filler for reuse
    src_core = src_tokens - _FILLER

    for our_norm, ch in our_by_norm.items():
        our_tokens = set(our_norm.split())
        if not our_tokens:
            continue
        our_core = our_tokens - _FILLER

        # 3. Token-set equality (with and without filler)
        if src_tokens == our_tokens:
            return ch
        if src_core and our_core and src_core == our_core:
            return ch

        # 4. Single-word core subset — only fire when the identifying word is
        #    meaningful (>= 4 chars).  Prevents "one"/"two"/"go" false-matches.
        if len(src_core) == 1:
            word = next(iter(src_core))
            if len(word) >= 4 and src_core.issubset(our_core):
                return ch
        if len(our_core) == 1:
            word = next(iter(our_core))
            if len(word) >= 4 and our_core.issubset(src_core):
                return ch

        # 5. One side's core is a subset of the other, diff only filler
        if our_core and our_core.issubset(src_tokens) and (src_tokens - our_core).issubset(_FILLER):
            return ch
        if src_core and src_core.issubset(our_tokens) and (our_tokens - src_core).issubset(_FILLER):
            return ch

    return None


# ---------------------------------------------------------------------------
# Build output XMLTV
# ---------------------------------------------------------------------------

def _xml_esc(s: str) -> str:
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _chno_sort_key(ch: dict):
    """Sort key: numeric tvg_chno, then alphabetically by name."""
    try:
        return (0, float(ch.get('tvg_chno') or 9999), ch.get('clean_name', ''))
    except (ValueError, TypeError):
        return (1, 9999, ch.get('clean_name', ''))


def _dummy_programmes(tvg_id: str, ch_name: str, chno: str = '') -> list[str]:
    """2-hour placeholder blocks for 7 days so Plex shows the channel name in the guide."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    # Align to the nearest 2-hour boundary
    now = now.replace(hour=(now.hour // 2) * 2)
    display_title = f"{chno} {ch_name}".strip() if chno else ch_name
    result = []
    for block in range(7 * 12):  # 7 days × 12 two-hour blocks
        start = now + timedelta(hours=block * 2)
        stop  = start + timedelta(hours=2)
        sfmt  = start.strftime('%Y%m%d%H%M%S +0000')
        efmt  = stop.strftime('%Y%m%d%H%M%S +0000')
        result.append(
            f'  <programme start="{sfmt}" stop="{efmt}" channel="{_xml_esc(tvg_id)}">'
            f'<title>{_xml_esc(display_title)}</title>'
            f'<sub-title>No Guide Data</sub-title>'
            f'<desc>Guide data is not available for this channel.</desc>'
            f'</programme>'
        )
    return result


def build_epg_xml(our_channels: list[dict], epg_sources: list) -> str:
    """
    Build XMLTV output:
    - channel id = our tvg-id (so Plex matches via GuideSourceID)
    - programme data pulled from matched source channels
    - channels sorted by tvg_chno so guide is in channel-number order
    - <lcn> and numbered display-name carry channel numbers into the guide
    - unmatched channels get placeholder entries so Plex can still stream them
    """
    # Build norm -> first-channel dict for matching, and norm -> all-channels for dup propagation
    our_by_norm: dict[str, dict]       = {}
    our_all_by_norm: dict[str, list]   = {}
    for ch in our_channels:
        n = ch['norm']
        if n and not ch.get('epg_skip'):
            our_all_by_norm.setdefault(n, []).append(ch)
            if n not in our_by_norm:
                our_by_norm[n] = ch

    matched: dict[str, dict] = {}          # tvg_id -> {'our': ch, 'src_id': str}
    programmes_by_src_id: dict[str, list] = {}  # src_id -> [ET.Element, ...]

    for src_channels, src_programmes in epg_sources:
        if src_channels is None:
            continue

        # Index source programmes by channel id upfront
        for prog in (src_programmes or []):
            sid = prog.get('channel', '')
            programmes_by_src_id.setdefault(sid, []).append(prog)

        for ch_elem in src_channels:
            src_id = ch_elem.get('id', '')
            # Try all display-name elements (some sources put alt names in extra ones)
            src_names = [dn.text or '' for dn in ch_elem.findall('display-name')]
            if not src_names:
                continue

            for src_name in src_names:
                src_name = src_name.strip()
                if not src_name:
                    continue
                src_norm = _normalize(src_name)
                our_ch = _find_match(src_norm, our_by_norm)
                if our_ch and our_ch['tvg_id'] not in matched:
                    matched[our_ch['tvg_id']] = {'our': our_ch, 'src_id': src_id}
                    logger.debug(
                        f"EPG match: '{our_ch['clean_name']}' <-> '{src_name}' (src_id={src_id})"
                    )
                    break  # stop trying more display-names for this source channel

    # Propagate matches to duplicate channels with the same name but different tvg-id
    # (same channel appearing across multiple filtered playlists)
    for our_norm, ch_list in our_all_by_norm.items():
        if len(ch_list) <= 1:
            continue
        src_id_for_group = next(
            (matched[ch['tvg_id']]['src_id'] for ch in ch_list if ch['tvg_id'] in matched),
            None
        )
        if src_id_for_group:
            for ch in ch_list:
                if ch['tvg_id'] not in matched:
                    matched[ch['tvg_id']] = {'our': ch, 'src_id': src_id_for_group}
                    logger.debug(f"EPG dup-match: '{ch['clean_name']}' reuses src_id={src_id_for_group}")

    # Build src_id -> list of our tvg_ids (one source can feed multiple dup channels)
    src_to_tvg_ids: dict[str, list[str]] = {}
    for tvg_id, m in matched.items():
        src_to_tvg_ids.setdefault(m['src_id'], []).append(tvg_id)

    # Sort all channels by channel number for ordered output
    all_sorted = sorted(our_channels, key=_chno_sort_key)
    matched_sorted   = [ch for ch in all_sorted if ch['tvg_id'] in matched]
    unmatched_sorted = [ch for ch in all_sorted if ch['tvg_id'] not in matched]

    # Assemble XML
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="iptv-manager">']

    # ---- channel elements (matched first, then unmatched) ----
    for ch in matched_sorted + unmatched_sorted:
        tvg_id = ch['tvg_id']
        chno   = (ch.get('tvg_chno') or '').strip()
        name   = ch['clean_name']
        parts.append(f'  <channel id="{_xml_esc(tvg_id)}">')
        # Single display-name: numbered when available (Plex sorts by this), plain otherwise
        display = f'{chno} {name}' if chno else name
        parts.append(f'    <display-name>{_xml_esc(display)}</display-name>')
        if chno:
            parts.append(f'    <lcn>{_xml_esc(chno)}</lcn>')
        logo = ch.get('logo') or ''
        if logo:
            parts.append(f'    <icon src="{_xml_esc(logo)}" />')
        parts.append('  </channel>')

    # ---- programme elements ----
    prog_count = 0
    for src_id, tvg_ids in src_to_tvg_ids.items():
        progs = programmes_by_src_id.get(src_id, [])
        for prog_elem in progs:
            for tvg_id in tvg_ids:
                prog_elem.set('channel', tvg_id)
                parts.append('  ' + ET.tostring(prog_elem, encoding='unicode'))
                prog_count += 1

    # ---- dummy programme blocks for unmatched channels ----
    for ch in unmatched_sorted:
        parts.extend(_dummy_programmes(ch['tvg_id'], ch['clean_name'], (ch.get('tvg_chno') or '').strip()))

    parts.append('</tv>')

    logger.info(
        f"EPG build complete: {len(matched)} matched, {len(unmatched_sorted)} unmatched, "
        f"{prog_count} programmes"
    )
    if unmatched_sorted:
        logger.debug(f"EPG unmatched channels: {[ch['clean_name'] for ch in unmatched_sorted]}")

    return '\n'.join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_and_cache_epg(item_ids: list[int] | None = None) -> str:
    """Fetch sources, build XMLTV, write cache, return XML string."""
    our_channels = get_channels_from_m3u(item_ids=item_ids)
    if not our_channels:
        logger.warning("EPG: no channels found in filtered M3U files — returning empty guide")
        return '<?xml version="1.0" encoding="UTF-8"?>\n<tv></tv>'

    sources_data = []

    # Provider EPG first (already on disk from M3U sync, no HTTP needed)
    for filename in sorted(os.listdir(config.M3U_DIR)):
        if filename.startswith("epg_") and filename.endswith(".xml"):
            path = os.path.join(config.M3U_DIR, filename)
            chs, progs = _load_epg_from_file(path)
            sources_data.append((chs, progs))

    # External sources as fallback for channels not covered by provider EPG
    for url in config.EPG_XML_SOURCES:
        chs, progs = _fetch_epg_source(url)
        sources_data.append((chs, progs))

    xml_content = build_epg_xml(our_channels, sources_data)

    os.makedirs(os.path.dirname(EPG_CACHE_PATH), exist_ok=True)
    tmp_cache = EPG_CACHE_PATH + ".tmp"
    with open(tmp_cache, 'w', encoding='utf-8') as f:
        f.write(xml_content)
    os.replace(tmp_cache, EPG_CACHE_PATH)
    write_count_to_cache(config.M3U_DIR, "generated", "epg_count", xml_content.count("<channel "), EPG_CACHE_PATH)
    logger.info(f"EPG cached at {EPG_CACHE_PATH} ({len(xml_content) // 1024} KB)")
    return xml_content


def get_epg(force_refresh: bool = False, item_ids: list[int] | None = None) -> str:
    """Return EPG XML, using on-disk cache unless stale or force_refresh=True."""
    if not force_refresh and os.path.exists(EPG_CACHE_PATH):
        age = time.time() - os.path.getmtime(EPG_CACHE_PATH)
        if age < EPG_CACHE_MAX_AGE:
            logger.debug(f"EPG: serving from cache (age {age / 3600:.1f}h)")
            with open(EPG_CACHE_PATH, 'r', encoding='utf-8') as f:
                return f.read()
    return build_and_cache_epg(item_ids=item_ids)


def run_epg_build(force_refresh: bool = True, item_ids: list[int] | None = None) -> None:
    """Entry point for running EPG builds in a separate process."""
    try:
        get_epg(force_refresh=force_refresh, item_ids=item_ids)
    except Exception as exc:
        logger.warning(f"EPG build failed: {exc}", exc_info=True)
