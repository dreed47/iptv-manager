"""
vpn_manager.py — OpenVPN subprocess lifecycle management.

Starts/stops openvpn as a daemon, polls for tun interface, reports status.
All files written to /tmp/vpn/ which is ephemeral and re-created on each start.
"""

import ipaddress
import logging
import os
import re
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


def _get_default_gw() -> tuple[str | None, str | None]:
    """Return (gateway_ip, interface) for the current default route."""
    try:
        out = subprocess.check_output(["ip", "route", "show", "default"], text=True)
        m = re.search(r"default via (\S+) dev (\S+)", out)
        if m:
            return m.group(1), m.group(2)
    except Exception:
        pass
    return None, None


def _get_iface_network(iface: str) -> str | None:
    """Return the subnet CIDR assigned to iface (e.g. '172.29.0.0/16')."""
    try:
        out = subprocess.check_output(
            ["ip", "-o", "-f", "inet", "addr", "show", iface], text=True
        )
        m = re.search(r"inet (\S+)", out)
        if m:
            return str(ipaddress.ip_interface(m.group(1)).network)
    except Exception:
        pass
    return None


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
            return False, (
                "/dev/net/tun not available. On the Docker host run: "
                "modprobe tun && echo 'tun' >> /etc/modules  "
                "then recreate the container (docker compose down && docker compose up -d)."
            )

        # Capture Docker routing state before OpenVPN overwrites the default route
        gw_ip, gw_iface = _get_default_gw()
        docker_net = _get_iface_network(gw_iface) if gw_iface else None
        logger.info(f"VPN: pre-VPN gateway={gw_ip} iface={gw_iface} docker_net={docker_net}")

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
                # OpenVPN pushes two catch-all routes (0.0.0.0/1 and 128.0.0.0/1 via tun0)
                # that cover all IPv4 space.  Without intervention, TCP responses to LAN
                # clients (e.g. the browser at 192.168.x.x) are sent into the tunnel and
                # never reach the browser — the TCP handshake never completes and the web
                # UI appears unreachable.
                #
                # Fix: add more-specific routes for all RFC 1918 private ranges via eth0.
                # The kernel prefers the more-specific /8, /12, /16 over VPN's /1 catch-alls,
                # so LAN/Docker traffic stays on eth0 while internet traffic goes via tun0.
                if gw_ip and gw_iface:
                    private_nets = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
                    for net in private_nets:
                        try:
                            subprocess.run(
                                ["ip", "route", "replace", net, "via", gw_ip, "dev", gw_iface],
                                check=True, capture_output=True,
                            )
                            logger.info(f"VPN: preserved LAN route {net} via {gw_ip} dev {gw_iface}")
                        except Exception as re_err:
                            logger.warning(f"VPN: could not add route for {net}: {re_err}")
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
