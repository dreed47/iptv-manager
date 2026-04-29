## Provider URL routing (Xtream proxy)


Each provider gets a URL slug (e.g. `my-provider`). Xtream clients connect at `/{slug}/player_api.php`. The slug routes to the correct `Item` row and uses that item's `proxy_username`/`proxy_password` for auth (not the global `IPTV_USERNAME`/`IPTV_PASSWORD` env vars, which are kept for backwards compat).
