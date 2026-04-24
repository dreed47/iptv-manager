"""
vpn_manager.py — OpenVPN subprocess lifecycle management.

Starts/stops openvpn as a daemon, polls for tun interface, reports status.
All files written to /tmp/vpn/ which is ephemeral and re-created on each start.
"""

import logging
import os
import shutil
import signal
import subprocess
import threading
import time

import requests as _requests

logger = logging.getLogger(__name__)

_VPN_DIR   = "/tmp/vpn"
_OVPN_PATH = "/tmp/vpn/surfshark.ovpn"
_AUTH_PATH = "/tmp/vpn/auth.txt"
_PID_PATH  = "/tmp/vpn/openvpn.pid"
_LOG_PATH  = "/tmp/vpn/openvpn.log"

_vpn_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_openvpn() -> str | None:
    """Return path to openvpn binary or None."""
    return shutil.which("openvpn") or (
        "/usr/sbin/openvpn" if os.path.exists("/usr/sbin/openvpn") else None
    )


def _tun_is_up() -> bool:
    """Return True if any tun interface exists."""
    try:
        for iface in os.listdir("/sys/class/net"):
            if iface.startswith("tun"):
                return True
    except Exception:
        pass
    return False


def _read_pid() -> int | None:
    try:
        with open(_PID_PATH) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _tail_log(n: int = 6) -> str:
    try:
        with open(_LOG_PATH) as f:
            lines = f.readlines()
        return " | ".join(l.strip() for l in lines[-n:] if l.strip())
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_vpn_status() -> dict:
    """Return current VPN status dict: {running, pid, interface}."""
    pid = _read_pid()
    alive = pid is not None and _process_alive(pid)
    tun_up = _tun_is_up()

    iface = None
    if tun_up:
        try:
            for name in os.listdir("/sys/class/net"):
                if name.startswith("tun"):
                    iface = name
                    break
        except Exception:
            pass

    return {
        "running": alive and tun_up,
        "pid": pid if alive else None,
        "interface": iface,
    }


def get_external_ip() -> str | None:
    """Fetch current external IP via api.ipify.org."""
    try:
        resp = _requests.get("https://api.ipify.org?format=json", timeout=10)
        resp.raise_for_status()
        return resp.json().get("ip")
    except Exception:
        return None


def start_vpn(config_str: str, username: str, password: str) -> tuple[bool, str]:
    """Start OpenVPN daemon. Returns (success, message)."""
    with _vpn_lock:
        # Already up?
        if get_vpn_status()["running"]:
            return True, "VPN already connected"

        # Prerequisites
        openvpn_bin = _find_openvpn()
        if not openvpn_bin:
            return False, (
                "openvpn binary not found — rebuild the Docker image "
                "(add 'openvpn' to apt-get install in Dockerfile)"
            )

        if not os.path.exists("/dev/net/tun"):
            # Create the tun device node — works when device_cgroup_rules grants
            # 'c 10:200 rwm' and the container has NET_ADMIN.
            try:
                os.makedirs("/dev/net", exist_ok=True)
                subprocess.run(
                    ["mknod", "/dev/net/tun", "c", "10", "200"],
                    check=True, capture_output=True,
                )
                os.chmod("/dev/net/tun", 0o666)
                logger.info("VPN: created /dev/net/tun device node")
            except Exception as e:
                return False, (
                    f"Cannot create /dev/net/tun: {e}. "
                    "Ensure 'cap_add: [NET_ADMIN]' and "
                    "'device_cgroup_rules: [c 10:200 rwm]' are set in docker-compose.yml "
                    "and recreate the container."
                )

        os.makedirs(_VPN_DIR, exist_ok=True)

        # Strip directives we'll inject via CLI args
        _strip = {"auth-user-pass", "daemon", "writepid", "log", "script-security"}
        config_lines = [
            line for line in config_str.splitlines()
            if not any(line.strip().lower().startswith(k) for k in _strip)
        ]
        with open(_OVPN_PATH, "w") as f:
            f.write("\n".join(config_lines) + "\n")

        # Write auth file (owner-read only)
        with open(_AUTH_PATH, "w") as f:
            f.write(f"{username}\n{password}\n")
        os.chmod(_AUTH_PATH, 0o600)

        # Clear old PID/log
        for p in (_PID_PATH, _LOG_PATH):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass

        # Launch daemon
        cmd = [
            openvpn_bin,
            "--config", _OVPN_PATH,
            "--auth-user-pass", _AUTH_PATH,
            "--writepid", _PID_PATH,
            "--log", _LOG_PATH,
            "--script-security", "2",
            "--daemon",
        ]
        logger.info(f"VPN: launching {' '.join(cmd[:3])} …")
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=8,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                return False, f"openvpn exited {proc.returncode}: {err}"
        except subprocess.TimeoutExpired:
            return False, "openvpn did not daemonize within 8s"
        except FileNotFoundError:
            return False, f"openvpn binary not found at {openvpn_bin}"

        # Poll for tun interface (up to 20s)
        for i in range(20):
            time.sleep(1)
            if _tun_is_up():
                pid = _read_pid()
                logger.info(f"VPN connected — tun up, pid={pid}")
                return True, "Connected"
            if i == 5:
                logger.info(f"VPN: still waiting for tun… log: {_tail_log(3)}")

        hint = _tail_log()
        return False, f"tun interface did not appear within 20s. Last log: {hint or '(empty)'}"


def stop_vpn() -> tuple[bool, str]:
    """Stop OpenVPN daemon. Returns (success, message)."""
    with _vpn_lock:
        pid = _read_pid()

        if pid is None:
            # Try pkill as fallback
            subprocess.run(["pkill", "-TERM", "-x", "openvpn"], capture_output=True)
            return True, "Stopped (no PID file; sent pkill)"

        if not _process_alive(pid):
            try:
                os.unlink(_PID_PATH)
            except FileNotFoundError:
                pass
            return True, "Stopped (process was already gone)"

        try:
            os.kill(pid, signal.SIGTERM)
        except PermissionError:
            return False, f"Permission denied sending SIGTERM to pid {pid}"

        # Wait for tun to go down
        for _ in range(10):
            time.sleep(1)
            if not _tun_is_up():
                break

        try:
            os.unlink(_PID_PATH)
        except FileNotFoundError:
            pass

        logger.info(f"VPN stopped, pid={pid}")
        return True, "Disconnected"
