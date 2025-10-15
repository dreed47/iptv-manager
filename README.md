# IPTV Manager

A web-based tool for managing, filtering, and serving IPTV playlists and EPG data, with Plex/HDHomeRun emulation for seamless integration.

## Features

- **Web UI**: Add, edit, and manage IPTV configurations via a simple browser interface.
- **M3U Playlist Fetching**: Download and store M3U playlists from Xtream Codes or M3U URLs.
- **Advanced Filtering**: Filter channels by language, includes, and excludes (with support for channel numbers and wildcards).
- **EPG Filtering**: Generate filtered EPG XML files based on selected channels.
- **HDHomeRun Emulation**: Emulates a HDHomeRun device for Plex Live TV compatibility.
- **Dockerized**: Easy to run and deploy with Docker Compose.

## How It Works

1. **Configuration**: Add IPTV server credentials and filtering rules in the web UI.
2. **Fetch Playlists**: Download the full playlist from your provider.
3. **Filter Playlists**: Apply language, include, and exclude rules to generate a filtered playlist.
4. **Serve to Plex**: Plex discovers the emulated HDHomeRun and streams filtered channels using the generated playlist and EPG.

## Usage

1. **Start the app**
   - With Docker Compose:
     ```sh
     docker-compose up --build
     ```
   - Or manually (requires Python 3.10+ and requirements.txt):
     ```sh
     pip install -r requirements.txt
     python main.py
     ```
2. **Open the Web UI**
   - Go to `http://localhost:5005` (or your configured host/port).
3. **Add/Edit IPTV Configurations**
   - Fill in server URL, username, password, and filtering options.
   - Use the "Includes" field for channel numbers and names (e.g., `100|ESPN`).
   - Save and fetch playlists/EPG as needed.
4. **Integrate with Plex**
   - Enable HDHomeRun discovery in the UI.
   - Add the discovered tuner in Plex Live TV setup.

## File Structure

- `main.py` - FastAPI app entry point
- `routes.py` - Main API and web routes
- `models.py` - Database models (SQLite via SQLAlchemy)
- `services.py` - CRUD and business logic
- `templates/index.html` - Web UI (Jinja2 template)
- `m3u_files/` - Stores downloaded and filtered playlists/EPGs
- `docker-compose.yml`, `Dockerfile` - Container setup

## Filtering Logic

- **Languages**: Only channels with matching language codes (from `tvg-name` prefix or first two chars) are included.
- **Includes**: Channels matching any substring (or `number|substring` for channel numbers) are included, even if excluded.
- **Excludes**: Channels matching any substring are excluded, unless also included.
- **Wildcard Exclude**: `*` in excludes means all channels are excluded unless explicitly included.

## Troubleshooting

- If Plex doesn't see the tuner, ensure discovery is enabled and ports are open.
- If filtering doesn't work as expected, check the includes/excludes formatting (one per line, or comma-separated).
- Logs are printed to the console for debugging.

## License

MIT
