# Deployment Guide

## Quick Start (Any Platform)

```sh
git clone https://github.com/dreed47/iptv-manager.git
cd iptv-manager
cp sample.env .env
```

Edit `.env` — at minimum set `HDHR_ADVERTISE_HOST` to your machine's LAN IP (or `127.0.0.1` if Plex runs on the same machine).

```sh
docker compose up -d
```

Open the web UI at `http://localhost:5005`.

---

## Environment Variables

| Variable              | Default          | Description |
|-----------------------|------------------|-------------|
| `APP_PORT`            | `5005`           | Host port Docker binds to. Change if running multiple instances. |
| `HDHR_ADVERTISE_HOST` | `127.0.0.1`      | IP address your media server uses to reach this app. Use your LAN IP if they run on separate machines. |
| `HDHR_SCHEME`         | `http`           | Protocol (`http` or `https`). |
| `HDHR_MODEL`          | `HDHR3-US`       | HDHomeRun model string reported to media servers. |
| `HDHR_FRIENDLY_NAME`  | `IPTV HDHomeRun` | Device name shown in Plex/Jellyfin/Emby. Useful when running multiple instances. |
| `HDHR_TUNER_COUNT`    | `2`              | Concurrent streams the media server may request. Raise for more simultaneous recordings. |
| `HDHR_DISABLE_SSDP`   | `1`              | `1` = SSDP disabled (default, works everywhere). `0` = SSDP enabled (Linux only, enables auto-discovery). |
| `EPG_XML_SOURCES`     | *(see below)*    | Comma-separated XMLTV URL(s) to fetch guide data from. Defaults to `https://epg.pw/xmltv/epg_US.xml`. |
| `EPG_CACHE_HOURS`     | `12`             | How long to cache the downloaded EPG before re-fetching. |

---

## EPG / Guide Data

The app builds a custom XMLTV file and serves it at `http://<host>:5005/epg.xml`. This is the recommended way to get programme guide data into Plex, Jellyfin, or Emby.

### How It Works

1. After every filter save, the EPG rebuilds automatically in the background (you can also trigger it manually via the **Refresh EPG** button).
2. Channel names in your filtered playlist are normalized (provider prefixes, HD/SD suffixes, and punctuation stripped) and matched against a source XMLTV feed by name.
3. Matched channels receive full programme schedules from the source.
4. Unmatched channels get 2-hour "No Guide Data" placeholder blocks so they still appear — and are streamable — in the guide grid.
5. The output file uses each channel's `tvg-id` as the XMLTV channel ID and includes `<lcn>` (logical channel number) elements so the guide sorts in channel-number order.

### EPG Source Configuration

Guide data is fetched from [epg.pw](https://epg.pw), a free service providing AI-generated XMLTV data for US and international channels.

```sh
# Default (US channels via epg.pw)
# No change needed in .env

# Different region — pick from https://epg.pw
EPG_XML_SOURCES=https://epg.pw/xmltv/epg_UK.xml

# Multiple sources merged (useful for multi-region lineups)
EPG_XML_SOURCES=https://epg.pw/xmltv/epg_US.xml,https://epg.pw/xmltv/epg_CA.xml

# Completely custom XMLTV feed (Schedules Direct, self-hosted, etc.)
EPG_XML_SOURCES=https://your-server.local/guide.xml
```

### Loop / Replay Channels

Channels with a `24/7:` style prefix (e.g., `24/7: THE BIG BANG THEORY`) are never matched against the EPG source — their looped broadcast schedule won't align with real programme data — and always receive placeholder "No Guide Data" entries.

### EPG Options — Choose One

| Option | Steps | When to use |
|--------|-------|-------------|
| **IPTV Manager EPG** (recommended) | Enter `http://<host>:5005/epg.xml` as the guide source in your media server's DVR/tuner settings | Works for all IPTV providers; full programme schedules via name-matching |
| **Plex location-based guide** | Skip the "Add Guide Source" step in Plex DVR wizard; let Plex map channels automatically | Plex Pass only; best when your lineup contains standard US broadcast/cable channels; Plex matches by channel number (`GuideNumber`) to its own guide database |
| **No guide data** | Skip the guide source step entirely | Channels are still streamable; guide grid shows no programme info |

#### Configuring Plex to Use the IPTV Manager EPG

1. Plex → Settings → Live TV & DVR → Set Up Plex DVR
2. Enter `http://<HDHR_ADVERTISE_HOST>:<APP_PORT>` as the device address.
3. After Plex detects the tuner and shows the channel list, on the **Guide Source** screen choose **Use a different guide source** or **Add XMLTV Guide** (wording varies by Plex version).
4. Enter: `http://<HDHR_ADVERTISE_HOST>:<APP_PORT>/epg.xml`
5. Plex fetches the EPG and matches programmes to channels via the `tvg-id` / `GuideSourceID` values — no manual channel mapping is required.

#### Using Plex's Location-Based Guide Instead

If your lineup is primarily US broadcast and cable channels and you have a Plex Pass:

1. In the Plex DVR wizard, after the tuner is detected, proceed through the channel list step without adding a custom EPG URL.
2. On the **Confirm Channels** screen, choose your country/postal code.
3. Plex maps channels by `GuideNumber`; make sure the channel numbers in your filtered playlist (`tvg-chno`) match the numbers Plex expects for those channels.
4. You can mix approaches: use Plex's built-in guide for broadcast channels and add `/epg.xml` for any that Plex can't map automatically.

---

## Channel Numbers

Channel numbers are preserved end-to-end from your provider to the guide.

### How Numbers Are Assigned

The `GuideNumber` that Plex sees (and that the `/auto/v<number>` stream URL uses) is determined as follows:

1. **Provider-supplied** (`tvg-chno` in the M3U `#EXTINF` line) — used directly as `GuideNumber`. These are treated as "explicit" and win any conflict.
2. **Auto-assigned** — Channels without a `tvg-chno` get the next available sequential integer (1, 2, 3…), skipping any numbers already claimed by explicit entries.
3. **Single configuration** — One IPTV configuration is supported. The filtered playlist is built from that single source.

### Channel Numbers in the EPG

The generated `/epg.xml` carries channel numbers in two places:
- `<display-name>` — prefixed with the channel number (e.g., `503 CNN`) so Plex sorts the guide grid correctly.
- `<lcn>` (Logical Channel Number) element — a standards-compliant way for media servers to read the channel number independently of the display name.

### Tips

- If your provider supplies `tvg-chno` values, those numbers will match what appears in your filtered playlist, the lineup, and the guide — no manual mapping needed.
- Channel numbers cannot be overridden via the includes list; they come from the `tvg-chno` attribute in your provider's M3U. Channels without a `tvg-chno` receive auto-assigned sequential numbers.
- Channel numbers in the HDHomeRun lineup also appear in the stream URL: `http://<host>:5005/auto/v<GuideNumber>` — this is what Plex calls when playing a channel.

---

## Connecting to Plex

### Manual (Recommended — works everywhere)

1. Open Plex → Settings → Live TV & DVR → Set Up Plex DVR
2. Choose **Enter device address manually**
3. Enter: `http://<HDHR_ADVERTISE_HOST>:<APP_PORT>` (e.g., `http://192.168.1.50:5005`)
4. Plex finds the tuner and channel list automatically.
5. On the guide source step, enter `http://<HDHR_ADVERTISE_HOST>:<APP_PORT>/epg.xml` — or choose Plex's location-based guide if preferred (see [EPG Options](#epg-options--choose-one) above).

### Connecting to Jellyfin

1. Jellyfin Dashboard → Live TV → Add Tuner Device
2. Tuner Type: **HDHomeRun**
3. Enter: `http://<HDHR_ADVERTISE_HOST>:<APP_PORT>`
4. Dashboard → Live TV → Add Guide Provider
5. Guide Provider: **XMLTV**; URL: `http://<HDHR_ADVERTISE_HOST>:<APP_PORT>/epg.xml`

### Connecting to Emby

1. Emby Dashboard → Live TV → Add Tuner Device
2. Tuner Type: **HDHomeRun**
3. Enter: `http://<HDHR_ADVERTISE_HOST>:<APP_PORT>`
4. Dashboard → Live TV → TV Guide Data Providers → Add → **XMLTV**
5. URL: `http://<HDHR_ADVERTISE_HOST>:<APP_PORT>/epg.xml`

### SSDP Auto-Discovery (Optional — Linux only)

SSDP lets media servers find the device without manual IP entry. Disabled by default because macOS Docker hangs for several minutes when binding UDP port 1900.

To enable on Linux:

1. Set `HDHR_DISABLE_SSDP=0` in `.env`
2. Uncomment the `1900:1900/udp` port line in `docker-compose.yml`
3. Restart: `./restart_container.sh`

Or toggle it at runtime from the web UI without editing any files.

---

## Media Server Documentation

- 📺 [Plex Live TV & DVR](https://support.plex.tv/articles/225877347-live-tv-dvr/)
- 🪼 [Jellyfin Live TV Setup](https://jellyfin.org/docs/general/server/live-tv/setup-guide)
- 🎬 [Emby Live TV Setup](https://support.emby.media/support/solutions/articles/44001160415-live-tv-setup)

---

## Rebuilding After Code Changes

```sh
./restart_container.sh             # incremental rebuild
./restart_container.sh --no-cache  # full clean rebuild
```

## Updating from Git

```sh
./update.sh             # git pull + incremental rebuild
./update.sh --no-cache  # git pull + full clean rebuild
```

---

## Troubleshooting

### Can't access web UI

```sh
# Check container status
docker compose ps

# Check logs
docker compose logs app

# Verify the port
curl http://localhost:5005/discover.json
```

### Media server can't connect

- Confirm `HDHR_ADVERTISE_HOST` in `.env` is the IP the media server can reach (not `127.0.0.1` if they're on separate machines).
- Test reachability: `curl http://<host>:5005/discover.json`
- Allow port 5005 through any firewall: `sudo ufw allow 5005/tcp`
- Confirm the tuner is listed at `http://<host>:5005/lineup.json` and the `URL` fields contain your machine's IP (not `127.0.0.1`).

### EPG / guide data missing

- The EPG rebuilds automatically after each filter save; allow ~2 minutes for the fetch to complete.
- Click the **Refresh EPG** button in the web UI to force an immediate rebuild.
- Check match results: `docker compose logs app | grep epg_manager`
- Channels with no matched guide data get "No Guide Data" placeholder entries — they remain streamable but the guide shows no programme info.
- Channels with a `24/7:` prefix are intentionally skipped for EPG matching and always show placeholder entries.
- If you're using Plex's location-based guide and channels aren't matching, verify that the channel numbers (`tvg-chno`) in your filtered playlist match what Plex expects for those channels in your region.

### Channel numbers wrong or duplicated

- Check the raw playlist for `tvg-chno` values in the `#EXTINF` lines. Channels with explicit numbers take priority over auto-assigned ones.
- If two configs contain the same channel with different explicit numbers, the first config's number wins.
- View the full lineup at `http://<host>:5005/lineup.json` to see exactly what `GuideNumber` each channel receives.

### Streams not playing

- Test a stream directly: `curl -I "http://<host>:5005/auto/v<GuideNumber>"`
- The app proxies all streams — it connects to your provider and forwards the data. If the provider URL is unreachable or the token has expired, re-fetch the M3U.
- Use the **Browse All Channels** → ▶ play button to test individual stream URLs directly before they go through the HDHomeRun proxy.
