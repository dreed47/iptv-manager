# IPTV Manager

A web-based tool for managing, filtering, and serving IPTV playlists and EPG data, with HDHomeRun emulation for seamless Plex, Jellyfin, and Emby Live TV integration.

## Features

- **Web UI** — Add, edit, and manage your IPTV configuration from a browser.
- **M3U Playlist Fetching** — Download playlists from Xtream Codes or direct M3U URLs.
- **Advanced Filtering** — Filter channels by language, keywords, channel numbers, and wildcards.
- **Full M3U Browser** — Browse and preview every channel in the raw (unfiltered) playlist before committing to a filter. Includes provider/category filtering, copy-to-clipboard, and an in-browser stream player for each channel.
- **Filtered Channel Viewer** — Browse your filtered channel lineup with the same provider/category drill-down and per-row stream playback.
- **EPG / Guide Data** — Fetches and name-matches guide data automatically; generates a standards-compliant XMLTV file with channel numbers. Unmatched channels receive placeholder "No Guide Data" entries so they remain streamable. EPG rebuilds automatically after each filter save.
- **Channel Number Preservation** — `tvg-chno` values from your provider are passed through to the HDHomeRun lineup and EPG so Plex shows the correct channel numbers.
- **HDHomeRun Emulation** — Appears as an HDHomeRun tuner on your network; Plex, Jellyfin, and Emby discover it automatically or via manual IP entry.
- **Stream Tester** — Test credentials and channel IDs directly from the browser before committing them to your config.
- **Health Check Endpoint** — `GET /api/health` returns stream reachability as JSON; integrates with Uptime Kuma (JSON Query monitor) and Home Assistant (REST sensor). Skips the test automatically when a live stream is active to avoid interruptions.
- **Docker** — Single `docker-compose.yml` works on macOS and Linux with no changes.

## Quick Start

```sh
git clone https://github.com/dreed47/iptv-manager.git
cd iptv-manager
cp sample.env .env
# Edit .env — set HDHR_ADVERTISE_HOST to your machine's LAN IP
docker compose up -d
```

Open the web UI at `http://localhost:5005` (or `http://<your-ip>:5005`).

See [DEPLOYMENT.md](DEPLOYMENT.md) for full setup, media server integration, EPG options, and configuration details.

## How It Works

1. **Add a config** — Enter your IPTV provider credentials (Xtream Codes or M3U URL) in the web UI.
2. **Fetch playlist** — Click **Fetch M3U** to download the full channel list from your provider.
3. **Browse (optional)** — Use **Browse All Channels** to explore the raw playlist and try playing streams before filtering.
4. **Filter** — Build your curated lineup using the **Includes** list. Add channel names (one per line) to select exactly which channels appear in your lineup. Click **Save Changes** to apply.
5. **View filtered lineup** — Click **Filtered Channel Viewer** to confirm the result.
6. **Connect your media server** — Point Plex/Jellyfin/Emby DVR at `http://<your-ip>:5005`; it discovers the device as an HDHomeRun tuner.
7. **Add the EPG** — In your media server's DVR settings, enter the EPG URL `http://<your-ip>:5005/epg.xml` so the guide populates automatically. See [EPG Options](#epg--guide-data) below for alternatives.

## Filtering Logic

The **Includes** list is the primary control. Add one channel name per line to select exactly which channels appear in your filtered lineup.

- **Includes present** — Only channels whose name is an exact match to an entry in the includes list are kept. Excludes are ignored when includes are set.
- **Excludes only** (no includes) — All channels pass through except those whose name contains an exclude substring.
- **Wildcard exclude (`*`)** — All channels start as excluded; only exact matches in the includes list are kept. Equivalent to "allow-list only" mode.

Saving filter changes resets the filtered playlist and triggers an automatic EPG rebuild, but leaves the original full playlist intact so you can re-filter without re-fetching.

## EPG / Guide Data

The app generates a custom XMLTV file at `http://<your-ip>:5005/epg.xml` that can be fed directly to Plex, Jellyfin, or Emby.

**How guide matching works:**
1. Channel names in your filtered playlist are normalized (stripped of provider prefixes, HD/SD suffixes, punctuation).
2. They are matched against names in one or more XMLTV source feeds (default: [epg.pw](https://epg.pw) US guide).
3. Matched channels receive full programme schedules. Unmatched channels get 2-hour "No Guide Data" placeholder blocks so they remain accessible in the guide.
4. The output uses each channel's `tvg-id` as the XMLTV channel ID, and includes `<lcn>` elements with channel numbers so media servers can sort the guide correctly.

**EPG options — choose one:**

| Option | When to use |
|--------|-------------|
| **Feed `/epg.xml` to your media server** | Best for most users. Full programme schedules, matched by channel name. Enter `http://<host>:5005/epg.xml` in your DVR/tuner EPG settings. |
| **Use Plex's location-based guide** | If you have a Plex Pass and your channels match known US broadcast channels. Plex matches channels by `GuideNumber` to its own guide data. No EPG URL needed — just skip the "Guide Source" step in Plex DVR setup. |
| **Custom XMLTV source** | Set `EPG_XML_SOURCES` in `.env` to point to any XMLTV feed (e.g., a regional epg.pw feed, Schedules Direct export, or self-hosted guide). |

**Loop/replay channels** (e.g., `24/7: THE BIG BANG THEORY`) are never matched against the EPG source — their broadcast schedule wouldn't align with a 24/7 loop stream — and always receive placeholder entries.

## Channel Numbers

Channel numbers flow through the entire stack so what you see in your IPTV app matches what appears in Plex/Jellyfin/Emby.

- **Provider-supplied numbers** (`tvg-chno` in the M3U `#EXTINF` line) are used as-is as the `GuideNumber` in the HDHomeRun lineup.
- **Auto-assigned numbers** — Channels without a `tvg-chno` get the next available sequential integer, starting from 1 and skipping any numbers already claimed by explicit entries.
- **Deduplication** — When the same channel appears in multiple configs, explicit-numbered entries win. If neither has an explicit number, the first occurrence is kept.
- **EPG ordering** — The generated `/epg.xml` sorts channels by channel number, and each `<channel>` element carries both a numbered `<display-name>` and an `<lcn>` element so the guide grid appears in channel-number order.

## File Structure

| File/Dir | Purpose |
|---|---|
| `main.py` | FastAPI app entry point |
| `routes.py` | Web and API routes |
| `hdhomerun_routes.py` | HDHomeRun emulation endpoints |
| `models.py` | SQLite database models |
| `services.py` | Business logic / CRUD |
| `epg_manager.py` | EPG fetching, name-matching, and XMLTV generation |
| `templates/index.html` | Main web UI |
| `templates/channels.html` | Channel browser UI |
| `templates/m3u_browser.html` | Full M3U browser UI |
| `templates/player.html` | In-browser stream player |
| `templates/stream_test.html` | Stream connection tester and health check config |
| `m3u_files/` | Downloaded playlists, filtered playlists, and cached EPG |
| `data/` | SQLite database |
| `docker-compose.yml` | Container definition |
| `restart_container.sh` | Rebuild and restart via docker compose |
| `update.sh` | Git pull + rebuild + restart |

## Media Server Setup

- 📺 [Plex Live TV & DVR](https://support.plex.tv/articles/225877347-live-tv-dvr/)
- 🪼 [Jellyfin Live TV Setup](https://jellyfin.org/docs/general/server/live-tv/setup-guide)
- 🎬 [Emby Live TV Setup](https://support.emby.media/support/solutions/articles/44001160415-live-tv-setup)

## License

MIT
