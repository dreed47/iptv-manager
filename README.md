# IPTV Manager

A web-based tool for managing, filtering, and serving IPTV playlists and EPG data, with HDHomeRun emulation for seamless Plex Live TV integration.

## Features

- **Web UI** — Add, edit, and manage IPTV configurations from a browser.
- **M3U Playlist Fetching** — Download playlists from Xtream Codes or direct M3U URLs.
- **Advanced Filtering** — Filter channels by language, keywords, channel numbers, and wildcards.
- **EPG / Guide Data** — Fetches and matches guide data automatically; generates XMLTV with channel numbers and dummy entries for unmatched channels. Loop/replay channels (`24/7:` prefix) are always given placeholder entries. EPG rebuilds automatically after each filter save.
- **HDHomeRun Emulation** — Appears as an HDHomeRun tuner to Plex Live TV.
- **Docker** — Single `docker-compose.yml` works on macOS and Linux with no changes.

## Quick Start

```sh
git clone https://github.com/dreed47/iptv-manager.git
cd iptv-manager
cp sample.env .env
# Edit .env — set HDHR_ADVERTISE_HOST to your machine's IP
docker compose up -d
```

Open the web UI at `http://localhost:5005` (or `http://<your-ip>:5005`).

See [DEPLOYMENT.md](DEPLOYMENT.md) for full setup, Plex integration, and configuration details.

## How It Works

1. **Add a config** — Enter your IPTV provider credentials in the web UI.
2. **Fetch playlist** — Download the full channel list from your provider.
3. **Filter** — Set language, include, and exclude rules to build your lineup.
4. **Connect Plex** — Point Plex DVR at `http://<your-ip>:5005`; it sees an HDHomeRun tuner.

## Filtering Logic

- **Languages** — Only channels with matching language codes are included.
- **Includes** — Channels matching any substring (or `number|name`) are always included.
- **Excludes** — Channels matching any substring are excluded, unless also in includes.
- **Wildcard Exclude** — `*` in excludes means all channels are excluded unless explicitly included.

## File Structure

| File/Dir | Purpose |
|---|---|
| `main.py` | FastAPI app entry point |
| `routes.py` | Web and API routes |
| `hdhomerun_routes.py` | HDHomeRun emulation endpoints |
| `models.py` | SQLite database models |
| `services.py` | Business logic / CRUD |
| `epg_manager.py` | EPG fetching, matching, and XMLTV generation |
| `templates/index.html` | Web UI |
| `m3u_files/` | Downloaded and filtered playlists/EPG cache |
| `data/` | SQLite database |
| `docker-compose.yml` | Container definition |
| `restart_container.sh` | Rebuild and restart via docker compose |
| `update.sh` | Git pull + rebuild + restart |

## License

MIT
