"""
hls_utils.py — HLS master playlist variant resolution.

Used by stream proxy paths to downselect to a lower-bitrate HLS variant when
HLS_MAX_BANDWIDTH_KBPS is configured, helping work around upstream bandwidth caps.
"""
import logging
import re
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)

_HLS_CONTENT_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
}

_STREAM_INF_RE = re.compile(r"#EXT-X-STREAM-INF:.*?BANDWIDTH=(\d+)", re.IGNORECASE)


def resolve_hls_variant(url: str, session: requests.Session, max_bw_kbps: int) -> str:
    """Return the best HLS variant URL at or below max_bw_kbps.

    Returns url unchanged if:
      - max_bw_kbps is 0
      - url is not an HLS master playlist
      - any error occurs during fetch/parse
    Falls back to lowest available variant if all exceed the threshold.
    """
    if not max_bw_kbps:
        return url

    try:
        resp = session.get(url, timeout=5, stream=True)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        is_hls_type = content_type in _HLS_CONTENT_TYPES
        is_hls_ext = url.split("?")[0].lower().endswith(".m3u8")

        if not is_hls_type and not is_hls_ext:
            return url

        body = resp.text

        # Chunklist (not master): first non-comment, non-empty line is #EXTINF or #EXT-X-TARGETDURATION
        first_tag = next(
            (ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("#EXTM3U")),
            "",
        )
        if first_tag and not first_tag.startswith("#EXT-X-STREAM-INF"):
            return url

        variants: list[tuple[int, str]] = []
        lines = body.splitlines()
        for i, line in enumerate(lines):
            m = _STREAM_INF_RE.match(line.strip())
            if m and i + 1 < len(lines):
                bw = int(m.group(1))
                variant_url = lines[i + 1].strip()
                if variant_url and not variant_url.startswith("#"):
                    if not variant_url.startswith("http"):
                        variant_url = urljoin(url, variant_url)
                    variants.append((bw, variant_url))

        if not variants:
            return url

        variants.sort(key=lambda x: x[0])
        threshold_bps = max_bw_kbps * 1000

        eligible = [(bw, u) for bw, u in variants if bw <= threshold_bps]
        if eligible:
            chosen_bw, chosen_url = eligible[-1]  # highest below threshold
        else:
            chosen_bw, chosen_url = variants[0]   # fallback: lowest available
            logger.warning(
                "HLS: all variants exceed %d kbps threshold; using lowest (%d kbps) for %s",
                max_bw_kbps, chosen_bw // 1000, url,
            )

        if chosen_url.lower().endswith(".m3u8"):
            logger.warning(
                "HLS: selected variant is still an .m3u8 chunklist — falling back to master URL. "
                "Full HLS segment proxying is not supported; set HLS_MAX_BANDWIDTH_KBPS=0 to disable."
            )
            return url

        logger.info(
            "HLS variant selected: %d kbps (threshold %d kbps) for %s",
            chosen_bw // 1000, max_bw_kbps, url,
        )
        return chosen_url

    except Exception as exc:
        logger.warning("HLS variant resolution failed for %s: %s", url, exc)
        return url
