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
| `HDHR_ADVERTISE_HOST` | `127.0.0.1`      | IP address Plex uses to reach the app. Use LAN IP if Plex is on another machine. |
| `HDHR_SCHEME`         | `http`           | Protocol (`http` or `https`). |
| `HDHR_MODEL`          | `HDHR3-US`       | HDHomeRun model string shown in Plex. |
| `HDHR_FRIENDLY_NAME`  | `IPTV HDHomeRun` | Device name shown in Plex. Useful for multiple instances. |
| `HDHR_TUNER_COUNT`    | `2`              | Concurrent streams Plex may request. Raise for more simultaneous recordings. |
| `HDHR_DISABLE_SSDP`   | `1`              | `1` = SSDP disabled (default, works everywhere). `0` = SSDP enabled (Linux only, enables Plex auto-discovery). |
| `EPG_XML_SOURCES`     | *(see below)*    | Comma-separated XMLTV URL(s) to fetch guide data from. Defaults to `https://epg.pw/xmltv/epg_US.xml`. |
| `EPG_CACHE_HOURS`     | `12`             | How long to cache the downloaded EPG before re-fetching. |

### EPG Source

Guide data is fetched from [epg.pw](https://epg.pw), a free service providing AI-generated XMLTV data for US channels, updated weekly. The app downloads the full feed, matches channels by name to your filtered playlist, and serves the result at `/epg.xml`.

Channels that don't match get placeholder "No Guide Data" entries so they remain streamable in Plex.

To use a different XMLTV source (e.g., for non-US channels):

```sh
# In .env — comma-separate multiple sources
EPG_XML_SOURCES=https://epg.pw/xmltv/epg_US.xml,https://example.com/other.xml
```

Other regional feeds from epg.pw: `epg_UK.xml`, `epg_CA.xml`, `epg_AU.xml`, etc. — see https://epg.pw for the full list.

---

## Connecting to Plex

### Manual (Recommended — works everywhere)

1. Open Plex → Settings → Live TV & DVR → Set Up Plex DVR
2. Choose **Enter device address manually**
3. Enter: `http://<HDHR_ADVERTISE_HOST>:<APP_PORT>` (e.g., `http://192.168.1.50:5005`)
4. Plex will find the tuner and guide automatically.

### SSDP Auto-Discovery (Optional — Linux only)

SSDP lets Plex find the device without manual configuration. It is disabled by default because macOS Docker hangs for several minutes when binding UDP port 1900.

To enable on Linux:

1. Set `HDHR_DISABLE_SSDP=0` in `.env`
2. Uncomment the `1900:1900/udp` port line in `docker-compose.yml`
3. Restart: `./restart_container.sh`

Or toggle it at runtime from the web UI without editing any files.

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

### Plex can't connect

- Confirm `HDHR_ADVERTISE_HOST` in `.env` is the IP Plex can reach (not `127.0.0.1` if Plex is on another machine).
- Test reachability from the Plex machine: `curl http://<host>:5005/discover.json`
- Allow port 5005 through any firewall: `sudo ufw allow 5005/tcp`

### EPG / guide data missing

- Click the **Refresh EPG** button in the web UI to force a fresh fetch.
- Check logs for match results: `docker compose logs app | grep epg_manager`
- Channels with no matched guide data get placeholder "No Guide Data" entries so they remain streamable in Plex.
