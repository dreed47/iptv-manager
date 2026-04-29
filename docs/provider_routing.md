## Provider URL routing (Xtream proxy)

### Credential-based routing (primary)

Each provider has unique `proxy_username` / `proxy_password` set on its edit page. IPTV apps connect to the root server URL — **no slug in the path**:

- `player_api.php?username=<proxy_user>&password=<proxy_pass>&action=…`
- `get.php?username=<proxy_user>&password=<proxy_pass>&…`
- `series/<proxy_user>/<proxy_pass>/<stream_id>.ext`
- `movie/<proxy_user>/<proxy_pass>/<stream_id>.ext`
- `live/<proxy_user>/<proxy_pass>/<stream_id>.ext`

The server looks up the correct `Item` row by matching `(proxy_username, proxy_password)` against all providers. If credentials are shared across providers, list actions (`get_series`, `get_vod_streams`, etc.) route to the first match; stream and info actions (`get_series_info`, stream URLs) additionally use the **stream ID namespace** to identify the correct provider unambiguously.

### Stream ID namespacing

Stream IDs encode the provider `item_id` so routes always resolve the correct provider even with shared credentials:

| Type | Formula | Example (item_id=2) |
|------|---------|---------------------|
| Live / VOD / Series | `item_id × 100_000_000 + offset + upstream_id` | `220_039_401` |
| Episode | `item_id × 1_000_000_000 + upstream_ep_id` | `2_001_662_558` |

Extraction: `item_id = stream_id // 1_000_000_000` (episodes) or `stream_id // 100_000_000` (others).

### Slug-based routing (legacy / advanced)

Slug routes (`/{slug}/player_api.php`, `/{slug}/series/…`, etc.) still work for apps that correctly pass the full server URL including path. Slugs are auto-generated from the provider name and stored internally but are no longer exposed in the UI.
