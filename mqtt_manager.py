"""
mqtt_manager.py — MQTT integration for IPTV Manager.

Publishes active stream state and VPN status to an MQTT broker.
Supports Home Assistant MQTT auto-discovery.
"""

import json
import logging
import threading
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton state
# ---------------------------------------------------------------------------

_manager: "MqttManager | None" = None
_manager_lock = threading.Lock()

# Per-provider publish targets: [{"item_id": int, "prefix": str, "device_name": str}]
_provider_configs: list[dict] = []


def set_provider_configs(configs: list[dict]) -> None:
    """Set the list of providers that should receive MQTT state publishes.
    Each entry: {"item_id": int, "prefix": str, "device_name": str}.
    Entries with a blank prefix are silently ignored.
    """
    global _provider_configs
    _provider_configs = [c for c in configs if c.get("prefix", "").strip()]
    logger.info(f"MQTT: provider configs updated ({len(_provider_configs)} provider(s) active)")

POLL_INTERVAL = 10      # seconds between state polls
HEARTBEAT_INTERVAL = 60 # seconds between forced publishes regardless of change
RECONNECT_DELAY = 30    # seconds to wait before first reconnect attempt after disconnect
RECONNECT_DELAY_MAX = 300  # cap backoff at 5 minutes


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_mqtt(config: dict) -> tuple[bool, str]:
    """Start (or restart) the MQTT manager with the given config dict."""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.disconnect()
            _manager = None
        mgr = MqttManager(config)
        ok, msg = mgr.connect()
        if ok:
            _manager = mgr
        return ok, msg


def stop_mqtt():
    """Stop the MQTT manager if running."""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.disconnect()
            _manager = None


def get_mqtt_status() -> dict:
    """Return current MQTT connection status."""
    with _manager_lock:
        if _manager is None:
            return {"connected": False, "broker": None}
        return _manager.get_status()


def retract_ha_discovery(prefix: str) -> bool:
    """Publish empty retained payloads to remove a device from HA discovery."""
    with _manager_lock:
        if _manager is None or not _manager._connected:
            return False
        _manager.unpublish_ha_discovery(prefix)
        return True


def publish_ha_discovery_for(prefix: str, device_name: str) -> bool:
    """Publish HA discovery payloads for any prefix/device_name pair."""
    with _manager_lock:
        if _manager is None or not _manager._connected:
            return False
        _manager.publish_ha_discovery_for(prefix, device_name)
        return True


def update_device_identity(prefix: str, device_name: str) -> bool:
    """Update the running manager's prefix/device_name and republish everything."""
    with _manager_lock:
        if _manager is None or not _manager._connected:
            return False
        _manager.update_device_identity(prefix, device_name)
        return True


def force_publish_state() -> None:
    """Reset poll state so the next tick immediately republishes streams/VPN to all prefixes."""
    with _manager_lock:
        if _manager is not None:
            _manager._prev_stream_count = -1
            _manager._prev_vpn_running = None
            _manager._last_heartbeat = 0.0


# ---------------------------------------------------------------------------
# MqttManager
# ---------------------------------------------------------------------------

class MqttManager:
    def __init__(self, config: dict):
        self._host = config.get("mqtt_host", "")
        self._port = int(config.get("mqtt_port") or 1883)
        self._username = config.get("mqtt_username") or None
        self._password = config.get("mqtt_password") or None
        self._prefix = (config.get("mqtt_topic_prefix") or "iptv-manager").rstrip("/")
        self._device_name = config.get("mqtt_device_name") or "IPTV Manager"

        self._connected = False
        self._client = None
        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._just_reconnected = False  # set by _on_connect; poll loop re-publishes state

        # Track previous state to detect changes
        self._prev_stream_count = -1
        self._prev_vpn_running = None
        self._last_heartbeat = 0.0

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> tuple[bool, str]:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            return False, "paho-mqtt not installed — add paho-mqtt to requirements.txt and rebuild"

        if not self._host:
            return False, "No MQTT host configured"

        # Unique client_id per prefix prevents broker-side collisions between instances
        uid = self._prefix.replace("/", "_").replace("-", "_")
        client = mqtt.Client(client_id=f"iptv-{uid}", clean_session=True)
        # Let paho handle reconnects with our backoff; on_connect fires _just_reconnected
        client.reconnect_delay_set(RECONNECT_DELAY, RECONNECT_DELAY_MAX)

        if self._username:
            client.username_pw_set(self._username, self._password)

        # Last Will — published automatically by broker if client disconnects uncleanly
        lwt_topic = f"{self._prefix}/status"
        client.will_set(lwt_topic, payload="offline", qos=1, retain=True)

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect

        try:
            client.connect(self._host, self._port, keepalive=60)
        except Exception as exc:
            return False, f"Could not connect to {self._host}:{self._port} — {exc}"

        client.loop_start()
        self._client = client

        # Wait up to 5s for the on_connect callback
        for _ in range(50):
            if self._connected:
                break
            time.sleep(0.1)

        if not self._connected:
            client.loop_stop()
            return False, f"Connected to {self._host}:{self._port} but no CONNACK received within 5s"

        # Publish online status to all configured provider prefixes
        for prefix in self._active_prefixes():
            self._publish(f"{prefix}/status", "online", retain=True)
        self.publish_ha_discovery()

        # Start polling thread
        self._stop_event.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name="mqtt-poll")
        self._poll_thread.start()

        logger.info(f"MQTT connected to {self._host}:{self._port} prefix={self._prefix}")
        return True, f"Connected to {self._host}:{self._port}"

    def disconnect(self):
        self._stop_event.set()
        if self._client:
            if self._connected:
                try:
                    for prefix in self._active_prefixes():
                        self._publish(f"{prefix}/status", "offline", retain=True)
                    self._client.disconnect()
                except Exception:
                    pass
            # Always stop the network thread — without this, the old paho loop
            # survives into reconnect-backoff state and collides with a new client.
            try:
                self._client.loop_stop()
            except Exception:
                pass
        self._connected = False
        logger.info("MQTT disconnected")

    def get_status(self) -> dict:
        return {
            "connected": self._connected,
            "broker": f"{self._host}:{self._port}" if self._host else None,
        }

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            self._just_reconnected = True  # poll loop will re-publish status on next tick
            logger.info(f"MQTT (re)connected to {self._host}:{self._port}")
        else:
            logger.warning(f"MQTT connect failed: rc={rc}")

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        if rc != 0:
            logger.warning(
                f"MQTT unexpected disconnect: rc={rc} — "
                f"paho will retry with backoff ({RECONNECT_DELAY}–{RECONNECT_DELAY_MAX}s)"
            )

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    def _poll_loop(self):
        while not self._stop_event.wait(POLL_INTERVAL):
            if not self._connected:
                continue
            try:
                self._poll_and_publish()
            except Exception as exc:
                logger.warning(f"MQTT poll error: {exc}")

    def _poll_and_publish(self):
        from hdhomerun_routes import get_active_streams
        from vpn_manager import get_vpn_status

        streams = get_active_streams()
        vpn = get_vpn_status()

        now = time.time()
        reconnected = self._just_reconnected
        self._just_reconnected = False
        force = reconnected or (now - self._last_heartbeat) >= HEARTBEAT_INTERVAL

        if reconnected:
            self.publish_ha_discovery()

        stream_count = len(streams)
        vpn_running = vpn.get("running", False)

        stream_changed = stream_count != self._prev_stream_count
        vpn_changed = vpn_running != self._prev_vpn_running

        if stream_changed or force:
            self._publish_streams(streams)
            self._prev_stream_count = stream_count

        if vpn_changed or force:
            self._publish_vpn(vpn)
            self._prev_vpn_running = vpn_running

        if force:
            for prefix in self._active_prefixes():
                self._publish(f"{prefix}/status", "online", retain=True)
            self._last_heartbeat = now

    # ------------------------------------------------------------------
    # Publish helpers
    # ------------------------------------------------------------------

    def _publish(self, topic: str, payload: str, retain: bool = False, qos: int = 1):
        if self._client and self._connected:
            self._client.publish(topic, payload=payload, qos=qos, retain=retain)

    def _active_prefixes(self) -> list[str]:
        """All prefixes that should receive publishes; falls back to self._prefix."""
        configs = list(_provider_configs)
        return [c["prefix"] for c in configs] if configs else [self._prefix]

    def _format_streams(self, streams: list) -> list:
        now = time.time()
        result = []
        for s in streams:
            elapsed = int(now - s.get("started_at", now))
            hours, rem = divmod(elapsed, 3600)
            mins, secs = divmod(rem, 60)
            duration = f"{hours}h {mins}m" if hours else f"{mins}m {secs}s"
            mb = round(s.get("bytes_sent", 0) / (1024 * 1024), 1)
            channel_num = s.get("channel", "?")
            channel_name = s.get("channel_name", "")
            channel_display = (
                f"{channel_num} — {channel_name}"
                if channel_name and channel_name != channel_num
                else channel_num
            )
            result.append({
                "session_id": s.get("session_id", ""),
                "channel": channel_display,
                "channel_number": channel_num,
                "channel_name": channel_name,
                "client_ip": s.get("client_ip", "?"),
                "user_agent": s.get("user_agent", ""),
                "duration": duration,
                "elapsed_seconds": elapsed,
                "mb_sent": mb,
                "started_at": int(s.get("started_at", now)),
            })
        return result

    def _publish_streams(self, streams: list):
        configs = list(_provider_configs)
        if configs:
            # Publish each provider's own stream count to its own prefix
            for cfg in configs:
                provider_streams = [s for s in streams if s.get("item_id") == cfg["item_id"]]
                formatted = self._format_streams(provider_streams)
                count = len(formatted)
                prefix = cfg["prefix"]
                self._publish(f"{prefix}/streams/count", str(count), retain=True)
                self._publish(f"{prefix}/streams/attributes", json.dumps({"count": count, "streams": formatted}), retain=True)
        else:
            formatted = self._format_streams(streams)
            count = len(formatted)
            self._publish(f"{self._prefix}/streams/count", str(count), retain=True)
            self._publish(f"{self._prefix}/streams/attributes", json.dumps({"count": count, "streams": formatted}), retain=True)

    def _publish_vpn(self, vpn: dict):
        running = vpn.get("running", False)
        state = "ON" if running else "OFF"
        attrs = json.dumps({"connected": running, "interface": vpn.get("interface")})
        for prefix in self._active_prefixes():
            self._publish(f"{prefix}/vpn/state", state, retain=True)
            self._publish(f"{prefix}/vpn/attributes", attrs, retain=True)

    # ------------------------------------------------------------------
    # Home Assistant auto-discovery
    # ------------------------------------------------------------------

    def update_device_identity(self, prefix: str, device_name: str):
        """Swap prefix/device_name in-place, publish online status, and republish discovery."""
        self._prefix = prefix.rstrip("/")
        self._device_name = device_name
        self._publish(f"{self._prefix}/status", "online", retain=True)
        # Reset poll state so streams/vpn are republished to the new prefix on the next tick
        self._prev_stream_count = -1
        self._prev_vpn_running = None
        self._last_heartbeat = 0.0
        self.publish_ha_discovery()

    def _discovery_topics(self, prefix: str) -> list[str]:
        uid_base = prefix.replace("/", "_").replace("-", "_")
        return [
            f"homeassistant/sensor/{uid_base}_streams/config",
            f"homeassistant/binary_sensor/{uid_base}_vpn/config",
            f"homeassistant/binary_sensor/{uid_base}_online/config",
        ]

    def unpublish_ha_discovery(self, prefix: str):
        """Retract all HA discovery entities for the given prefix (empty retained payload)."""
        for topic in self._discovery_topics(prefix):
            self._publish(topic, "", retain=True)
        logger.info(f"MQTT: retracted HA discovery for prefix={prefix}")

    def publish_ha_discovery_for(self, prefix: str, device_name: str):
        uid_base = prefix.replace("/", "_").replace("-", "_")
        device = {"name": device_name or prefix, "identifiers": [uid_base]}
        avail = {
            "availability_topic": f"{prefix}/status",
            "payload_available": "online",
            "payload_not_available": "offline",
        }
        entities = [
            (
                f"homeassistant/sensor/{uid_base}_streams/config",
                {
                    "name": "Active Streams",
                    "state_topic": f"{prefix}/streams/count",
                    "json_attributes_topic": f"{prefix}/streams/attributes",
                    "unit_of_measurement": "streams",
                    "icon": "mdi:television-play",
                    "unique_id": f"{uid_base}_streams",
                    "device": device,
                    **avail,
                },
            ),
            (
                f"homeassistant/binary_sensor/{uid_base}_vpn/config",
                {
                    "name": "VPN",
                    "state_topic": f"{prefix}/vpn/state",
                    "json_attributes_topic": f"{prefix}/vpn/attributes",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "device_class": "connectivity",
                    "icon": "mdi:vpn",
                    "unique_id": f"{uid_base}_vpn",
                    "device": device,
                    **avail,
                },
            ),
            (
                f"homeassistant/binary_sensor/{uid_base}_online/config",
                {
                    "name": "Online",
                    "state_topic": f"{prefix}/status",
                    "payload_on": "online",
                    "payload_off": "offline",
                    "device_class": "connectivity",
                    "unique_id": f"{uid_base}_online",
                    "device": device,
                },
            ),
        ]
        for topic, payload in entities:
            self._publish(topic, json.dumps(payload), retain=True)
        logger.info(f"MQTT: published HA discovery for {len(entities)} entities (prefix={prefix})")

    def publish_ha_discovery(self):
        configs = list(_provider_configs)
        if configs:
            for cfg in configs:
                self.publish_ha_discovery_for(cfg["prefix"], cfg["device_name"])
        else:
            self.publish_ha_discovery_for(self._prefix, self._device_name)


# ---------------------------------------------------------------------------
# One-shot test helper (used by /mqtt/test route)
# ---------------------------------------------------------------------------

def test_mqtt_connection(config: dict) -> tuple[bool, str]:
    """Attempt a connect/publish/disconnect without starting the background thread."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        return False, "paho-mqtt not installed"

    host = config.get("mqtt_host", "")
    port = int(config.get("mqtt_port") or 1883)
    username = config.get("mqtt_username") or None
    password = config.get("mqtt_password") or None
    prefix = (config.get("mqtt_topic_prefix") or "iptv-manager").rstrip("/")

    if not host:
        return False, "No host configured"

    connected = threading.Event()
    result: list = []

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            connected.set()
        else:
            result.append(f"CONNACK rc={rc}")

    client = mqtt.Client(client_id="iptv-manager-test", clean_session=True)
    if username:
        client.username_pw_set(username, password)
    client.on_connect = on_connect

    try:
        client.connect(host, port, keepalive=10)
    except Exception as exc:
        return False, f"Could not connect to {host}:{port} — {exc}"

    client.loop_start()
    ok = connected.wait(timeout=5)
    if not ok:
        client.loop_stop()
        err = result[0] if result else "no CONNACK within 5s"
        return False, f"Failed: {err}"

    client.publish(f"{prefix}/status", "online-test", qos=1)
    time.sleep(0.3)
    client.disconnect()
    client.loop_stop()
    return True, f"Successfully connected to {host}:{port}"
