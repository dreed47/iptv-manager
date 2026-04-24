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

| Variable | Default | Description |
|---|---|---|
| `APP_PORT` | `5005` | Host port Docker binds to. Change if running multiple instances. |
| `CONTAINER_NAME` | `iptv-app` | Docker container name. Change when running multiple instances to avoid name conflicts. |
| `RESTART_POLICY` | `unless-stopped` | Docker restart policy. Set to `no` to prevent auto-restart (e.g. during maintenance). Valid values: `no`, `always`, `unless-stopped`, `on-failure`. |
| `TZ` | `UTC` | Container timezone used for log timestamps. Set to your local tz database name, e.g. `America/New_York`, `America/Chicago`, `Europe/London`. |
| `HDHR_ADVERTISE_HOST` | `127.0.0.1` | IP address your media server uses to reach this app. Use your LAN IP if they run on separate machines. |
| `HDHR_SCHEME` | `http` | Protocol (`http` or `https`). |
| `HDHR_MODEL` | `HDHR3-US` | HDHomeRun model string reported to media servers. |
| `HDHR_FRIENDLY_NAME` | `IPTV HDHomeRun` | Device name shown in Plex/Jellyfin/Emby. Useful when running multiple instances. |
| `HDHR_TUNER_COUNT` | `2` | Concurrent streams the media server may request. Raise for more simultaneous recordings. |
| `HDHR_DISABLE_SSDP` | `1` | `1` = SSDP disabled (default, works everywhere). `0` = SSDP enabled (Linux only, enables auto-discovery). |
| `IPTV_USERNAME` | `iptv` | Username IPTV apps use to connect **to this app** (not your upstream provider). Used by TiviMate, IPTV Smarters, VLC, etc. |
| `IPTV_PASSWORD` | `iptv` | Password IPTV apps use to connect **to this app** (not your upstream provider). |
| `EPG_XML_SOURCES` | *(see below)* | Comma-separated XMLTV URL(s) to fetch guide data from. Defaults to `https://epg.pw/xmltv/epg_US.xml`. |
| `EPG_CACHE_HOURS` | `12` | How long to cache the downloaded EPG before re-fetching. |
| `ALLOW_FULL_M3U_DOWNLOAD` | `1` | Set to `0` to disable the **Fetch M3U** button in the UI. Useful on shared/production deployments to prevent accidental re-fetches. |
| `STREAM_CHUNK_KB` | `64` | Size in KB of each chunk read from the upstream source. Larger values (128, 256) reduce overhead; too large may increase latency. |
| `STREAM_PREBUFFER_KB` | `512` | KB to buffer server-side before sending to the client. Increase (e.g. 1024, 2048) if streams stutter at startup. Set to `0` to disable. |
| `STREAM_MAX_RETRIES` | `5` | How many times the proxy reconnects if the stream drops. Set to `0` to disable auto-reconnect. |
| `STREAM_RETRY_DELAY` | `3` | Seconds to wait between reconnect attempts. |
| `STREAM_READ_TIMEOUT` | `30` | Seconds without incoming data before the proxy considers the stream stale and reconnects. Lower values (e.g. `15`) catch silent hangs faster. |
| `STREAM_SESSION_STALE_SECONDS` | `30` | Seconds since the last received chunk before a session is considered dead. Used by the health check to avoid false "stream active" reports after a client disconnects. |
| `PLAYER_STASH_KB` | `1024` | mpegts.js client-side stash buffer in KB for the in-browser player. Larger values absorb more upstream jitter. |
| `PLAYER_LATENCY_MAX` | `30` | Max live buffer latency in seconds before the browser player skips ahead (in-browser player only). |
| `PLAYER_LATENCY_MIN` | `5` | Minimum buffer to keep in seconds (in-browser player only). |
| `HEALTH_CHECK_TVG_ID` | *(none)* | Channel TVG-ID used by `GET /api/health` for Uptime Kuma / Home Assistant probes. Pick a reliable, low-priority channel. See [Monitoring](#monitoring). |

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
|---|---|---|
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
3. **Multiple configurations** — When more than one IPTV config is active, channels are merged and de-duplicated across all configs. Explicit-numbered entries win conflicts; otherwise the first occurrence is kept.

### Channel Numbers in the EPG

The generated `/epg.xml` carries channel numbers in two places:

- `<display-name>` — prefixed with the channel number (e.g., `503 CNN`) so Plex sorts the guide grid correctly.
- `<lcn>` (Logical Channel Number) element — a standards-compliant way for media servers to read the channel number independently of the display name.

### Tips

- If your provider supplies `tvg-chno` values, those numbers will match what appears in your filtered playlist, the lineup, and the guide — no manual mapping needed.
- Channel numbers cannot be overridden via the includes list; they come from the `tvg-chno` attribute in your provider's M3U. Channels without a `tvg-chno` receive auto-assigned sequential numbers.
- Channel numbers in the HDHomeRun lineup also appear in the stream URL: `http://<host>:5005/auto/v<GuideNumber>` — this is what Plex calls when playing a channel.

---

## Generic IPTV App Proxy (Xtream Codes)

In addition to HDHomeRun emulation for Plex/Jellyfin/Emby, the app acts as a full **Xtream Codes API server** so generic IPTV players (TiviMate, IPTV Smarters Pro, GSE Smart IPTV, VLC, etc.) can connect directly and browse live channels, VOD, and series.

### Connecting an IPTV App

In your IPTV app's "Add Playlist / Add Source" screen, choose **Xtream Codes** and enter:

| Field | Value |
|---|---|
| Server | `http://<HDHR_ADVERTISE_HOST>:<APP_PORT>` |
| Username | value of `IPTV_USERNAME` in `.env` (default: `iptv`) |
| Password | value of `IPTV_PASSWORD` in `.env` (default: `iptv`) |

> **Note:** `IPTV_USERNAME` and `IPTV_PASSWORD` are the credentials your IPTV app uses to connect **to this app** — they are not your upstream provider credentials. The upstream credentials are stored per-config in the web UI.

Alternatively, some apps accept an M3U URL directly:

```text
http://<host>:<port>/get.php?username=iptv&password=iptv&type=m3u_plus
```

or a simple playlist URL (no auth required if credentials are default):

```text
http://<host>:<port>/iptv/playlist.m3u
```

### What the IPTV App Sees

- **Live channels** — your filtered lineup, proxied through `/live/…`
- **VOD (movies)** — all movie entries from your provider's full playlist
- **Series** — all series entries with full season/episode metadata fetched from your provider

### Extra Channels for IPTV Apps Only (`xtream_includes`)

The **Xtream Extra Channels** field in each config's edit form lets you include additional live channels that appear in the IPTV app but are **not** added to your HDHomeRun/Plex lineup. This is useful for channels you want accessible in TiviMate but don't want cluttering your Plex guide.

Enter comma-separated name patterns (wildcards supported):

```text
*ESPN*, *HBO*, *Showtime*
```

---

## Scheduled M3U Refresh

Each config has an **Auto-refresh interval** field (in hours). When set to a non-zero value, the app automatically re-fetches and re-filters the playlist on that schedule — useful when your provider issues new stream URLs or tokens periodically.

Set it to `0` (or leave blank) to disable automatic refreshes and fetch manually via the **Fetch M3U** button.

---

## OpenVPN

The app can route all outbound container traffic (connections to your IPTV provider) through an OpenVPN tunnel. This is useful if your provider performs better over VPN or if you want to conceal IPTV traffic from your ISP.

> **Scope:** VPN affects only outbound traffic from the container (to your IPTV provider). Inbound connections from Plex, IPTV apps, and your browser reach the container via Docker's bridge network and are unaffected — your local devices always connect to this app on your LAN IP directly.

### Docker Requirements

The provided `docker-compose.yml` already includes everything needed:

```yaml
cap_add:
  - NET_ADMIN
devices:
  - /dev/net/tun:/dev/net/tun
```

If you modified your compose file and removed these, add them back and **recreate** (not just restart) the container:

```sh
docker compose down && docker compose up -d
```

### Setup

1. Navigate to **IPTV Provider** (Settings) in the web UI.
2. Expand the **OpenVPN** section.
3. Paste the full contents of an `.ovpn` config file from your VPN provider into the text area.
4. Enter your VPN **service credentials** (username and password).

   > Most VPN providers issue separate *service credentials* for manual/OpenVPN connections — these are different from your account login email and password. Check your VPN provider's manual setup documentation.

5. Click **Save VPN Settings**.
6. Click **Enable VPN**. The status indicator turns green when the tunnel is up.

### Controls

| Action | Where |
|---|---|
| Enable / Disable VPN | Settings → OpenVPN section, or Dashboard → Quick Actions |
| Check connection status | Dashboard → OpenVPN stat card (live, updates every 10 s) |
| Test your exit IP | Settings → OpenVPN → **Test Connection** (shows the public IP the provider sees) |
| VPN per-stream indicator | Dashboard → Active Streams (each row shows 🔒 VPN or Direct) |

### Auto-Start on Container Restart

If VPN was enabled when the container stopped, it reconnects automatically on the next startup — no manual action required.

### Stream Tester and VPN

The **Stream Tester** page makes test requests from the container, so they go through the VPN when it is active. Some IPTV providers front their authentication endpoints with Cloudflare, which may block known VPN exit IPs. If the tester reports a Cloudflare error, this does **not** necessarily mean your streams are broken — actual stream URLs (sourced from your M3U file) connect to direct stream servers that are usually not behind Cloudflare and work fine over VPN.

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

## Auto-Restart on Unhealthy Container (autoheal)

The app's `docker-compose.yml` includes a Docker healthcheck that polls `http://localhost:5005/` every 30 seconds. If the container becomes **unhealthy**, Docker will not restart it on its own — but [autoheal](https://github.com/willfarrell/autoheal) can.

autoheal is a lightweight sidecar container that watches all containers with a healthcheck and automatically restarts any that enter an `unhealthy` state.

> **One instance per host:** Only one autoheal container should run on a given Docker host. If you already have autoheal running for other containers, do not start a second — your existing instance will cover IPTV Manager automatically as long as `AUTOHEAL_CONTAINER_LABEL=all` is set.

### Running autoheal

An example Compose file is provided at [`autoheal_example_docker_compose.yaml`](autoheal_example_docker_compose.yaml). You can run it alongside IPTV Manager using Docker's multiple-file override:

```sh
docker compose -f docker-compose.yml -f autoheal_example_docker_compose.yaml up -d
```

Or add the `autoheal` service block directly into your `docker-compose.yml`:

```yaml
  autoheal:
    image: willfarrell/autoheal
    container_name: autoheal
    restart: unless-stopped
    environment:
      - AUTOHEAL_CONTAINER_LABEL=all
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
```

`AUTOHEAL_CONTAINER_LABEL=all` tells autoheal to watch every container that has a healthcheck. To restrict it to only IPTV Manager, add the label `autoheal: "true"` to the `app` service in `docker-compose.yml` and set `AUTOHEAL_CONTAINER_LABEL=autoheal` instead.

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
- The proxy automatically detects token-expiry errors (HTTP 407) and session-rejection errors (HTTP 458) and refreshes the channel lineup for a fresh URL before retrying.
- Use **Browse Full M3U** → ▶ play button to test individual stream URLs directly before they go through the HDHomeRun proxy.

### VPN won't connect

- Confirm the container has the required capabilities. Run `docker inspect iptv-app | grep -A5 CapAdd` — you should see `NET_ADMIN`. If not, recreate the container (`docker compose down && docker compose up -d`) after verifying `cap_add` and `devices` are present in `docker-compose.yml`.
- Check OpenVPN logs from the web UI (Tools → Logs) or via `docker compose logs app | grep vpn`.
- Confirm you are using **service credentials**, not your VPN account login. Most providers generate a separate username/password for manual OpenVPN connections.
- The `.ovpn` config must be a valid OpenVPN config file. Test it with a desktop OpenVPN client first if you are unsure.

### Stream Tester fails when VPN is on

This is expected in many cases. The Stream Tester opens a connection to your provider's authentication endpoint, which may be fronted by Cloudflare. Cloudflare blocks requests from known VPN/datacenter IPs. This does not mean your actual streams are broken — stream URLs from your M3U file connect to direct stream servers that are not behind Cloudflare and work normally through VPN. The error modal in the Stream Tester will indicate when a Cloudflare block is detected.

---

## Monitoring

The app exposes a health-check endpoint at `GET /api/health` that external tools can poll to determine if your IPTV provider is reachable.

### How it works

1. If a proxy stream is already active, the endpoint returns `{"status": "skipped"}` (HTTP 200) instead of running a test. This prevents interrupting a live stream and avoids false alerts.
2. Otherwise it opens a connection to your provider, reads the first 4 KB of data, then immediately closes — the same check the stream_test page performs manually.
3. Returns `{"status": "ok"}` (HTTP 200) on success, or `{"status": "down"}` (HTTP 503) on failure.

### Monitoring Setup

1. Find the TVG-ID of a reliable, low-priority channel (visible on the Stream Tester page).
2. Add it to `.env`:

   ```sh
   HEALTH_CHECK_TVG_ID=12345
   ```

3. Rebuild the container.

You can also override the channel per-request with `?tvg_id=12345`, or target a specific config with `?item_id=1`.

### Uptime Kuma

| Field | Value |
|---|---|
| Monitor type | **HTTP(S) — JSON Query** |
| URL | `http://<host>:5005/api/health` |
| JSON Query | `$.status` |
| Expected value | `ok` |

`skipped` returns HTTP 200 so Uptime Kuma won't alert while you're actively watching live TV.

### Home Assistant

Add a REST sensor to `configuration.yaml`:

```yaml
sensor:
  - platform: rest
    name: IPTV Status
    resource: http://<host>:5005/api/health
    value_template: "{{ value_json.status }}"
    json_attributes:
      - latency_ms
      - active_streams
    scan_interval: 60
```

The sensor state will be `ok`, `down`, or `skipped`. Use it in automations or a dashboard card. Example automation trigger:

```yaml
trigger:
  - platform: state
    entity_id: sensor.iptv_status
    to: "down"
```
