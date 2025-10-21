# Deployment Guide

## Universal Configuration

**Good news: The same `docker-compose.yml` works perfectly on both macOS and Debian/Linux!**

No platform-specific changes needed. Just run:

```bash
docker-compose up -d
```

---

## SSDP Auto-Discovery (Optional)

SSDP/UPnP auto-discovery is **optional** - all HDHomeRun features work without it!

### Option 1: Manual Plex Configuration (Recommended)

Just add your server URL in Plex DVR settings:

```
http://192.168.86.254:5005
```

✅ Works on all platforms  
✅ No configuration needed  
✅ Instant startup

### Option 2: Enable SSDP Auto-Discovery

If you want Plex to **automatically** discover your device:

**On Debian/Linux:**

1. Click "Enable Discovery" button in web UI
2. Plex will find it automatically

**On macOS:**
⚠️ Not recommended - causes 4-5 minute startup delay due to Docker bug

---

## Quick Start

### Any Platform (macOS, Debian, Ubuntu, etc.)

```bash
git clone <repo>
cd iptv-manager
docker-compose up -d

# Access web UI
open http://localhost:5005
# or
open http://<your-server-ip>:5005
```

**That's it!** Everything works out of the box.

---

## Features

| Feature                   | Status                          |
| ------------------------- | ------------------------------- |
| Web UI                    | ✅ Works                        |
| HTTP API endpoints        | ✅ Works                        |
| M3U playlist generation   | ✅ Works                        |
| EPG filtering             | ✅ Works                        |
| Manual Plex configuration | ✅ Works                        |
| SSDP auto-discovery       | 🔘 Optional (enable via web UI) |
| Startup time              | ⚡ Instant                      |

---

## Environment Variables

| Variable              | Default          | Description                            |
| --------------------- | ---------------- | -------------------------------------- |
| `HDHR_ADVERTISE_HOST` | `192.168.86.254` | Your server's IP address               |
| `HDHR_ADVERTISE_PORT` | `5005`           | HTTP port                              |
| `HDHR_SCHEME`         | `http`           | Protocol                               |
| `HDHR_DISABLE_SSDP`   | `1`              | SSDP disabled (safe for all platforms) |

---

## Troubleshooting

### Can't access web UI

```bash
# Check if container is running
docker-compose ps

# Check logs
docker-compose logs app

# Verify IP address matches your server
# Edit docker-compose.yml and change HDHR_ADVERTISE_HOST

# Check firewall allows port 5005
# macOS: System Preferences > Security & Privacy > Firewall
# Linux: sudo ufw allow 5005/tcp
```

### Plex can't connect

```bash
# Add manually in Plex DVR settings:
http://<HDHR_ADVERTISE_HOST>:5005

# Test the discovery endpoint:
curl http://192.168.86.254:5005/discover.json

# Should return JSON with device info
```

### Want to enable SSDP auto-discovery?

**On Linux/Debian:**

1. Open web UI at http://your-server-ip:5005
2. Click "Enable Discovery" button
3. Plex will auto-discover the device

**On macOS:**
Not recommended - will cause 4-5 minute delays. Use manual Plex configuration instead.

---

## Advanced: Permanent SSDP on Linux Only

If you're **only** deploying on Linux and want SSDP auto-enabled:

Edit `docker-compose.yml`:

```yaml
environment:
  - HDHR_DISABLE_SSDP=0 # Change 1 to 0

ports:
  - "5005:5005"
  - "1900:1900/udp" # Uncomment this
```

Then verify:

```bash
docker-compose logs app | grep SSDP

# Should show:
# INFO: SSDP socket.bind completed in 0.001s
```

**But honestly?** The web UI button is easier and more flexible.
