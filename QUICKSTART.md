# Quick Start

## Installation (Any Platform)

Works on macOS, Debian, Ubuntu, and any Linux distribution:

```bash
git clone https://github.com/dreed47/iptv-manager.git
cd iptv-manager
docker-compose up -d
```

Access the web UI at: `http://localhost:5005` (or your server's IP)

That's it! No platform-specific configuration needed.

---

## Connecting to Plex

### Option 1: Manual Configuration (Recommended)

1. Open Plex Settings → Live TV & DVR
2. Add DVR Device manually
3. Enter: `http://192.168.86.254:5005` (use your server's IP)
4. Done!

### Option 2: Auto-Discovery (Optional, Linux only)

1. Open the web UI at `http://your-server-ip:5005`
2. Click "Enable Discovery" button
3. Plex will automatically find the device

**Note:** Auto-discovery is not recommended on macOS due to Docker networking limitations. Manual configuration works perfectly on all platforms.

---

## Features

✅ Web-based configuration UI  
✅ M3U playlist generation from Xtream API  
✅ Channel filtering (by language, keywords, includes/excludes)  
✅ EPG (Electronic Program Guide) filtering  
✅ HDHomeRun emulation for Plex integration  
✅ Works on macOS, Linux, and Windows (via Docker)  
✅ No platform-specific configuration needed

---

## Configuration

All settings are managed through the web UI:

1. **Add IPTV Provider:** Server URL, username, password
2. **Fetch M3U:** Download channel list from provider
3. **Filter Channels:** Set languages, includes/excludes with channel numbers
4. **Generate Filtered Playlist:** Create your custom channel lineup
5. **Connect to Plex:** Use the generated playlist URL

See [DEPLOYMENT.md](DEPLOYMENT.md) for detailed documentation.

---

## Platform Notes

- **macOS:** Works perfectly! SSDP auto-discovery disabled to prevent startup delays.
- **Debian/Linux:** Works perfectly! Enable SSDP via web UI if you want auto-discovery.
- **All Platforms:** Manual Plex configuration works identically everywhere.

See [PLATFORM-NOTES.md](PLATFORM-NOTES.md) for the full story.
