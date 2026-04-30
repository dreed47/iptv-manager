# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Features

A web-based tool for managing, filtering, and serving IPTV playlists and EPG data.  The two core features are:  

* HDHomeRun Tuner emulation - To allow user to expose IPTV channels to media players like Plex, Jellyfin, and Emby.  
* IPTV Provider Proxies - To allow user to define one or more IPTV backend providers and manage playlist and EPG data to then serve to your IPTV Apps. 

## Running the app

```bash
# Docker (normal usage)
cp sample.env .env          # edit HDHR_ADVERTISE_HOST at minimum
docker compose up -d

# Local dev (no Docker)
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 5005 --reload
```

App runs on port 5005 by default. No build step — Python runs directly.

## Architecture
See [docs/architecture.md](docs/architecture.md) for full system design.

## Two separate filter systems — do not confuse

`item.includes` — HDHomeRun channel list, format `100|ESPN,200|FOX`. Used only by `build_filter_config` → `apply_m3u_filter` → `filtered_playlist_{id}.m3u`. Controls what Plex/Jellyfin/Emby sees.

`item.xtream_includes` — Wildcard patterns like `*ESPN*,*Fox News*`. Used only by `xtream_server_routes.py` to filter the Xtream Codes API responses (TiviMate, IPTV Smarters, etc.). Does NOT affect the filtered M3U.

## Async rules
See [docs/async.md](docs/async.md) for important async rules. 

## Provider URL routing (Xtream proxy)
See [docs/provider_routing.md](docs/provider_routing.md) for provider routing details.


## EPG system

See [docs/epg.md](docs/epg.md) for EPG build pipeline, channel mapping, and sample starter channel/filter lists.

## Env vars worth knowing
See [docs/env_vars.md](docs/env_vars.md) for key environment variables used by the app.

