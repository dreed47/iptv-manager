# Platform Notes

## The Simple Truth

**One configuration works everywhere!** 🎉

The current `docker-compose.yml` is already optimized for **all platforms**:

- macOS ✅
- Debian ✅
- Ubuntu ✅
- Any Linux ✅

No changes needed!

---

## About SSDP Auto-Discovery

SSDP (Simple Service Discovery Protocol) is what allows Plex to **automatically find** your HDHomeRun device without manual configuration.

### Do You Actually Need It?

**Probably not!** Here's why:

#### Without SSDP:

1. Open Plex
2. Add DVR manually: `http://192.168.86.254:5005`
3. Done! ✅

#### With SSDP:

1. Open Plex
2. It finds the device automatically
3. Done! ✅

**Same result, but manual is 5 seconds of clicking.**

---

## Why SSDP is Disabled by Default

### On macOS:

- Docker has a networking bug with UDP port 1900
- Binding to this port hangs for **4-5 minutes** 😱
- So we disable it to keep startup instant

### On Debian/Linux:

- No such bug - port 1900 works perfectly
- But **you still don't need it** - manual config works great
- If you want it: Just click "Enable Discovery" in the web UI!

---

## When You Actually Want SSDP

Enable SSDP if:

- ✅ You have multiple HDHomeRun devices
- ✅ You frequently rebuild your Plex server
- ✅ You just think auto-discovery is cool

Don't bother if:

- ✅ You only have one device
- ✅ You set it up once and forget it
- ✅ You prefer simple, predictable configurations

---

## How to Enable SSDP (If You Want It)

### Easy Way (Works on Linux):

1. Open web UI at `http://your-server:5005`
2. Click "Enable Discovery" button
3. Done!

### Permanent Way (Linux Only):

Edit `docker-compose.yml`:

```yaml
- HDHR_DISABLE_SSDP=0 # Change to 0
- "1900:1900/udp" # Uncomment
```

**On macOS:** Don't bother - it'll work but cause delays.

---

## Summary

✅ **Current config works perfectly on all platforms**  
✅ **SSDP is optional** - manual Plex config is just as good  
✅ **Enable SSDP via web UI** if you really want it (Linux only recommended)  
✅ **No platform-specific changes needed!**
