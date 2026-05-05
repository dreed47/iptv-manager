"""
network_test_routes.py — VPN / network quality test for IPTV fitness.
"""
import asyncio
import logging
import math
import os
import socket
import threading
import time
import urllib.parse

import requests
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import config
from models import Item, SessionLocal

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_NETWORK_TEST_LOCK = threading.Lock()
_NETWORK_TEST_INFLIGHT = False
_CURRENT_RUN_ID: str | None = None
_test_results: dict[str, dict] = {}
_MAX_STORED_RUNS = 5

_BASELINE_HOST = "1.1.1.1"
_BASELINE_PORT = 443
_BASELINE_DL_URL_3MB = "https://speed.cloudflare.com/__down?bytes=3145728"
_BASELINE_DL_URL_2MB = "https://speed.cloudflare.com/__down?bytes=2097152"

_TEST_DEFINITIONS = [
    {"name": "latency",      "label": "Latency (TCP)"},
    {"name": "bandwidth",    "label": "Bandwidth Estimate"},
    {"name": "packet_loss",  "label": "Packet Loss Proxy"},
    {"name": "stream_pull",  "label": "Stream Pull"},
    {"name": "jitter",       "label": "Jitter Estimate"},
]

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/tools/network_test", response_class=HTMLResponse)
async def network_test_page(
    request: Request,
    provider_id: int = None,
    run_id: str = None,
):
    def _load():
        with SessionLocal() as db:
            items = db.query(Item).all()
            return [{"id": it.id, "name": it.name} for it in items]

    providers = await asyncio.to_thread(_load)

    selected = provider_id
    if selected is None and providers:
        selected = providers[0]["id"]

    template = templates.get_template("network_test.html")
    rendered = template.render({
        "request": request,
        "providers": providers,
        "selected_provider_id": selected,
        "run_id": run_id or "",
        "active_page": "tools",
    })
    return HTMLResponse(content=rendered)


class _StartBody(BaseModel):
    provider_id: int


@router.post("/api/network_test/start")
async def api_start_network_test(body: _StartBody):
    global _NETWORK_TEST_INFLIGHT, _CURRENT_RUN_ID

    with _NETWORK_TEST_LOCK:
        if _NETWORK_TEST_INFLIGHT:
            return JSONResponse(
                status_code=409,
                content={"error": "Test already running", "run_id": _CURRENT_RUN_ID},
            )
        run_id = str(int(time.time() * 1000))
        _NETWORK_TEST_INFLIGHT = True
        _CURRENT_RUN_ID = run_id

    _test_results[run_id] = {
        "run_id": run_id,
        "provider_id": body.provider_id,
        "provider_name": None,
        "started_at": time.time(),
        "finished_at": None,
        "status": "running",
        "error": None,
        "vpn_active": None,
        "vpn_interface": None,
        "tests": [
            {"name": t["name"], "label": t["label"], "status": "pending", "result": None}
            for t in _TEST_DEFINITIONS
        ],
    }

    thread = threading.Thread(
        target=_run_network_tests,
        args=(run_id, body.provider_id),
        daemon=True,
    )
    thread.start()

    return JSONResponse({"run_id": run_id})


@router.get("/api/network_test/status")
async def api_network_test_status(run_id: str):
    entry = _test_results.get(run_id)
    if entry is None:
        return JSONResponse(status_code=404, content={"error": "Run not found"})
    return JSONResponse(entry)


@router.get("/api/network_test/inflight")
async def api_network_test_inflight():
    return JSONResponse({"inflight": _NETWORK_TEST_INFLIGHT, "run_id": _CURRENT_RUN_ID})


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _run_network_tests(run_id: str, provider_id: int):
    global _NETWORK_TEST_INFLIGHT, _CURRENT_RUN_ID

    try:
        if provider_id == -1:
            import vpn_manager
            vpn_status = vpn_manager.get_vpn_status()
            _test_results[run_id]["vpn_active"] = vpn_status.get("running", False)
            _test_results[run_id]["vpn_interface"] = vpn_status.get("interface", "")
            _test_results[run_id]["provider_name"] = "Baseline (Cloudflare)"
            _run_latency_test(run_id, _BASELINE_HOST, _BASELINE_PORT)
            _run_baseline_bandwidth(run_id)
            _run_packet_loss_test(run_id, _BASELINE_HOST, _BASELINE_PORT)
            _run_baseline_stream_pull(run_id)
            _run_jitter_test(run_id, _BASELINE_HOST, _BASELINE_PORT)
            _test_results[run_id]["status"] = "done"
            _test_results[run_id]["finished_at"] = time.time()
            return

        with SessionLocal() as db:
            item = db.query(Item).filter(Item.id == provider_id).first()
            if item is None:
                _test_results[run_id]["status"] = "error"
                _test_results[run_id]["error"] = f"Provider {provider_id} not found"
                return
            server_url = (item.server_url or "").rstrip("/")
            username = item.username or ""
            user_pass = item.user_pass or ""
            _test_results[run_id]["provider_name"] = item.name

        import vpn_manager
        vpn_status = vpn_manager.get_vpn_status()
        _test_results[run_id]["vpn_active"] = vpn_status.get("running", False)
        _test_results[run_id]["vpn_interface"] = vpn_status.get("interface", "")

        parsed = urllib.parse.urlparse(server_url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        _run_latency_test(run_id, host, port)
        _run_bandwidth_test(run_id, server_url, username, user_pass, provider_id)
        _run_packet_loss_test(run_id, host, port)
        _run_stream_pull_test(run_id, server_url, username, user_pass, provider_id)
        _run_jitter_test(run_id, host, port)

        _test_results[run_id]["status"] = "done"
        _test_results[run_id]["finished_at"] = time.time()

    except Exception as exc:
        logger.warning("network_test run %s fatal error: %s", run_id, exc)
        _test_results[run_id]["status"] = "error"
        _test_results[run_id]["error"] = str(exc)

    finally:
        with _NETWORK_TEST_LOCK:
            _NETWORK_TEST_INFLIGHT = False
            _CURRENT_RUN_ID = None
        _evict_old_runs()


# ---------------------------------------------------------------------------
# Per-test functions
# ---------------------------------------------------------------------------

def _run_latency_test(run_id: str, host: str, port: int):
    _set_test_status(run_id, "latency", "running")
    samples = []
    for _ in range(3):
        t0 = time.monotonic()
        try:
            sock = socket.create_connection((host, port), timeout=5)
            sock.close()
            samples.append((time.monotonic() - t0) * 1000)
        except (socket.timeout, OSError):
            pass

    if not samples:
        _set_test_result(run_id, "latency", "error", {
            "error": "All TCP connect attempts failed",
        })
        return

    min_ms = min(samples)
    avg_ms = sum(samples) / len(samples)
    max_ms = max(samples)
    status = "pass" if avg_ms < 100 else ("warn" if avg_ms <= 200 else "fail")
    _set_test_result(run_id, "latency", status, {
        "min_ms": round(min_ms, 1),
        "avg_ms": round(avg_ms, 1),
        "max_ms": round(max_ms, 1),
        "attempts": len(samples),
        "detail": f"avg {round(avg_ms, 1)}ms  (min {round(min_ms, 1)} / max {round(max_ms, 1)})",
        "threshold_label": "avg < 100ms pass · 100–200ms warn · > 200ms fail",
    })


def _run_bandwidth_test(run_id: str, server_url: str, username: str, user_pass: str, provider_id: int):
    _set_test_status(run_id, "bandwidth", "running")
    headers = {"User-Agent": "VLC/3.0.18 LibVLC/3.0.18", "Accept": "*/*"}
    TARGET = 3 * 1024 * 1024  # 3 MB

    # Candidate URLs in preference order. get.php returns the full M3U (2-10 MB,
    # good for throughput measurement). Falls back to a live stream for Xtream-only
    # providers that don't expose get.php.
    candidates = [
        f"{server_url}/get.php?username={username}&password={user_pass}&type=m3u_plus&output=ts",
    ]
    stream_url = _find_first_stream_url(provider_id)
    if stream_url:
        candidates.append(stream_url)

    last_error = None
    for url in candidates:
        try:
            t0 = time.monotonic()
            resp = requests.get(url, stream=True, timeout=20, headers=headers)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            if "text/html" in ct:
                resp.close()
                last_error = "Server returned HTML (Cloudflare block?)"
                continue
            bytes_read = 0
            for chunk in resp.iter_content(chunk_size=65536):
                bytes_read += len(chunk)
                if bytes_read >= TARGET:
                    break
            resp.close()
            elapsed = max(time.monotonic() - t0, 0.01)
            if bytes_read < 10_000:
                last_error = f"Response too small ({bytes_read} bytes)"
                continue
            mbps = (bytes_read * 8) / (elapsed * 1_000_000)
            status = "pass" if mbps > 20 else ("warn" if mbps >= 10 else "fail")
            _set_test_result(run_id, "bandwidth", status, {
                "mbps": round(mbps, 2),
                "bytes_downloaded": bytes_read,
                "elapsed_s": round(elapsed, 3),
                "detail": f"{round(mbps, 2)} Mbps  ({bytes_read / (1024*1024):.1f} MB in {round(elapsed, 2)}s)",
                "threshold_label": "> 20Mbps pass · 10–20Mbps warn · < 10Mbps fail",
            })
            return
        except Exception as exc:
            last_error = str(exc)

    _set_test_result(run_id, "bandwidth", "error", {
        "error": last_error or "All bandwidth sources failed",
    })


def _run_packet_loss_test(run_id: str, host: str, port: int):
    _set_test_status(run_id, "packet_loss", "running")
    ATTEMPTS = 10
    failures = 0
    for _ in range(ATTEMPTS):
        try:
            sock = socket.create_connection((host, port), timeout=3)
            sock.close()
        except (socket.timeout, OSError):
            failures += 1

    loss_pct = (failures / ATTEMPTS) * 100
    status = "pass" if loss_pct == 0 else ("warn" if loss_pct <= 5 else "fail")
    _set_test_result(run_id, "packet_loss", status, {
        "attempts": ATTEMPTS,
        "failures": failures,
        "loss_pct": round(loss_pct, 1),
        "detail": f"{failures}/{ATTEMPTS} failed ({round(loss_pct, 1)}%)",
        "threshold_label": "0% pass · 1–5% warn · > 5% fail",
    })


def _run_stream_pull_test(
    run_id: str, server_url: str, username: str, user_pass: str, provider_id: int
):
    _set_test_status(run_id, "stream_pull", "running")

    stream_url = _find_first_stream_url(provider_id)
    if not stream_url:
        _set_test_result(run_id, "stream_pull", "error", {
            "error": "No M3U playlist found — fetch M3U for this provider first",
            "stream_url": None,
        })
        return

    TARGET_BYTES = 2 * 1024 * 1024
    headers = {"User-Agent": "VLC/3.0.18 LibVLC/3.0.18", "Accept": "*/*"}
    try:
        t0 = time.monotonic()
        resp = requests.get(stream_url, stream=True, timeout=15, headers=headers)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "text/html" in ct:
            resp.close()
            _set_test_result(run_id, "stream_pull", "fail", {
                "error": "Server returned HTML instead of stream data (Cloudflare block?)",
                "stream_url": stream_url,
                "bytes_read": 0,
                "elapsed_s": 0,
                "mbps": 0,
            })
            return
        bytes_read = 0
        for chunk in resp.iter_content(chunk_size=65536):
            bytes_read += len(chunk)
            if bytes_read >= TARGET_BYTES:
                break
        resp.close()
        elapsed = max(time.monotonic() - t0, 0.01)
        mbps = (bytes_read * 8) / (elapsed * 1_000_000)
        status = "pass" if mbps > 10 else ("warn" if mbps >= 5 else "fail")
        _set_test_result(run_id, "stream_pull", status, {
            "mbps": round(mbps, 2),
            "bytes_read": bytes_read,
            "elapsed_s": round(elapsed, 3),
            "stream_url": stream_url,
            "error": None,
            "detail": f"{round(mbps, 2)} Mbps  ({bytes_read / (1024 * 1024):.1f} MB in {round(elapsed, 2)}s)",
            "threshold_label": "> 10Mbps pass · 5–10Mbps warn · < 5Mbps fail",
        })
    except Exception as exc:
        _set_test_result(run_id, "stream_pull", "error", {
            "error": str(exc),
            "stream_url": stream_url,
        })


def _run_jitter_test(run_id: str, host: str, port: int):
    _set_test_status(run_id, "jitter", "running")
    samples = []
    for _ in range(5):
        t0 = time.monotonic()
        try:
            sock = socket.create_connection((host, port), timeout=5)
            sock.close()
            samples.append((time.monotonic() - t0) * 1000)
        except (socket.timeout, OSError):
            pass

    if len(samples) < 2:
        _set_test_result(run_id, "jitter", "error", {
            "error": f"Insufficient samples ({len(samples)}/5) for jitter calculation",
        })
        return

    mean = sum(samples) / len(samples)
    variance = sum((s - mean) ** 2 for s in samples) / len(samples)
    stddev = math.sqrt(variance)
    status = "pass" if stddev < 10 else ("warn" if stddev <= 30 else "fail")
    _set_test_result(run_id, "jitter", status, {
        "samples_ms": [round(s, 1) for s in samples],
        "stddev_ms": round(stddev, 2),
        "variance_ms": round(variance, 2),
        "mean_ms": round(mean, 1),
        "detail": f"stddev {round(stddev, 2)}ms over {len(samples)} samples",
        "threshold_label": "< 10ms pass · 10–30ms warn · > 30ms fail",
    })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_test_status(run_id: str, test_name: str, status: str):
    for entry in _test_results[run_id]["tests"]:
        if entry["name"] == test_name:
            entry["status"] = status
            break


def _set_test_result(run_id: str, test_name: str, status: str, result_dict: dict):
    for entry in _test_results[run_id]["tests"]:
        if entry["name"] == test_name:
            entry["status"] = status
            entry["result"] = result_dict
            break


def _find_first_stream_url(provider_id: int) -> str | None:
    for filename in [
        f"filtered_playlist_{provider_id}.m3u",
        f"xtream_playlist_{provider_id}.m3u",
    ]:
        path = os.path.join(config.M3U_DIR, filename)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            if not line.startswith("#EXTINF"):
                continue
            lower = line.lower()
            if 'group-title="vod"' in lower or 'group-title="series"' in lower:
                continue
            if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                return lines[i + 1].strip()
    return None


def _run_baseline_bandwidth(run_id: str):
    _set_test_status(run_id, "bandwidth", "running")
    TARGET = 3 * 1024 * 1024
    headers = {"User-Agent": "VLC/3.0.18 LibVLC/3.0.18", "Accept": "*/*"}
    try:
        t0 = time.monotonic()
        resp = requests.get(_BASELINE_DL_URL_3MB, stream=True, timeout=20, headers=headers)
        resp.raise_for_status()
        bytes_read = 0
        for chunk in resp.iter_content(chunk_size=65536):
            bytes_read += len(chunk)
            if bytes_read >= TARGET:
                break
        resp.close()
        elapsed = max(time.monotonic() - t0, 0.01)
        mbps = (bytes_read * 8) / (elapsed * 1_000_000)
        status = "pass" if mbps > 20 else ("warn" if mbps >= 10 else "fail")
        _set_test_result(run_id, "bandwidth", status, {
            "mbps": round(mbps, 2),
            "bytes_downloaded": bytes_read,
            "elapsed_s": round(elapsed, 3),
            "detail": f"{round(mbps, 2)} Mbps  ({bytes_read / (1024*1024):.1f} MB in {round(elapsed, 2)}s) — Cloudflare CDN",
            "threshold_label": "> 20Mbps pass · 10–20Mbps warn · < 10Mbps fail",
        })
    except Exception as exc:
        _set_test_result(run_id, "bandwidth", "error", {"error": str(exc)})


def _run_baseline_stream_pull(run_id: str):
    _set_test_status(run_id, "stream_pull", "running")
    TARGET_BYTES = 2 * 1024 * 1024
    headers = {"User-Agent": "VLC/3.0.18 LibVLC/3.0.18", "Accept": "*/*"}
    try:
        t0 = time.monotonic()
        resp = requests.get(_BASELINE_DL_URL_2MB, stream=True, timeout=15, headers=headers)
        resp.raise_for_status()
        bytes_read = 0
        for chunk in resp.iter_content(chunk_size=65536):
            bytes_read += len(chunk)
            if bytes_read >= TARGET_BYTES:
                break
        resp.close()
        elapsed = max(time.monotonic() - t0, 0.01)
        mbps = (bytes_read * 8) / (elapsed * 1_000_000)
        status = "pass" if mbps > 10 else ("warn" if mbps >= 5 else "fail")
        _set_test_result(run_id, "stream_pull", status, {
            "mbps": round(mbps, 2),
            "bytes_read": bytes_read,
            "elapsed_s": round(elapsed, 3),
            "stream_url": _BASELINE_DL_URL_2MB,
            "error": None,
            "detail": f"{round(mbps, 2)} Mbps  ({bytes_read / (1024 * 1024):.1f} MB in {round(elapsed, 2)}s) — CDN proxy (baseline)",
            "threshold_label": "> 10Mbps pass · 5–10Mbps warn · < 5Mbps fail",
        })
    except Exception as exc:
        _set_test_result(run_id, "stream_pull", "error", {
            "error": str(exc),
            "stream_url": _BASELINE_DL_URL_2MB,
        })


def _evict_old_runs():
    if len(_test_results) > _MAX_STORED_RUNS:
        oldest_keys = sorted(_test_results.keys())[:-_MAX_STORED_RUNS]
        for k in oldest_keys:
            del _test_results[k]
