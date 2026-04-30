## EPG (Electronic Program Guide)

The EPG builder generates a single merged XMLTV file (`generated_epg.xml`) that Plex/Jellyfin/Emby can use for guide data.

### How it works

1. Reads `hdhr_filtered_playlist.m3u` (the global HDHR channel list) to get the channel set.
2. Fetches XMLTV data from one or more source URLs (`EPG_XML_SOURCES`).
3. Matches each channel to a source channel using the strategy below.
4. Outputs an XMLTV where `channel id` = our `tvg-id`, so Plex can auto-match via `GuideSourceID`.
5. Unmatched channels get 2-hour placeholder blocks so they still appear in the guide.

EPG is rebuilt automatically when the cache is older than `EPG_CACHE_HOURS` (default 12h) or when triggered manually from the HDHomeRun page.

### Match strategy (in priority order)

| Priority | Method | Notes |
|----------|--------|-------|
| 1 | **Explicit override** | Set via EPG Channel Mapping page — always wins |
| 2 | **tvg-id match** | Our M3U `tvg-id` == source `<channel id>` — cleanest match |
| 3 | **Normalized name match** | Strips provider prefix (`US:`, `SLING:`, `GO:`, `PRIME:`, `24/7:`), HD/SD suffix, punctuation; handles aliases |
| 4 | **Dup propagation** | Same channel appearing with different tvg-ids across providers inherits an already-matched result |

`24/7:` loop channels are skipped during name matching (no broadcast schedule to match) but still get placeholder blocks.

### EPG Channel Mapping page

**URL:** `/epg/channel-map` (linked from the HDHomeRun page)

Use this when auto-matching picks the wrong channel or fails entirely. Enter the `<channel id>` value from your EPG source XML (e.g. `espn.us`, `cnn.us`) in the override field for that channel. Save triggers an immediate EPG rebuild.

Match status badges:
- **tvg-id ✓** — matched by direct tvg-id comparison
- **Name ✓** — matched by normalized display name
- **Dup match** — inherited from another instance of the same channel
- **Override** — explicit mapping you set
- **No match** — will get placeholder blocks only

To find valid EPG source IDs, open `/epg.xml` and search for `<channel id="...">` values.

### Sample HDHomeRun channel filter list

Paste into the **HDHomeRun Channel List** field (`item.includes` format: `number|Name`):

```
3|US: CBS 2 (KDKA) PITTSBURGH HD
6|US: ABC 4 (WTAE)
9|PRIME: WPXI PITTSBURGH (NBC)
12|US: FOX 53 (WPGH) PITTSBURGH HD
15|US: FOX NEWS HD
18|US: NEWSMAX HD
21|SLING: NEWSNATION
24|US: MLB NETWORK
27|SLING: MLB NETWORK
30|US: NFL NETWORK HD
33|US: NFL REDZONE
36|US: ESPN HD
39|US: ESPN 2 HD
42|SLING: NHL NETWORK
45|US: DIY HD
48|US: TCM HD
51|US: MOTORTREND HD
54|US: TV LAND HD
57|US: THE WEATHER CHANNEL HD
60|GO: METV
63|US: GRIT
66|US: ANTENNA TV
69|SLING: PARAMOUNT NETWORK
72|US: USA NETWORK 4K
75|US: SYFY HD
78|US: TNT HD
81|US: TBS HD
84|US: COMEDY CENTRAL HD
87|GO: FX MOVIE CHANNEL
90|GO: NATIONAL GEOGRAPHIC CHANNEL
93|SLING: OUTDOOR CHANNEL
96|US: TRAVEL CHANNEL HD
99|US: DISCOVERY HD
102|GO: TLC
105|GO: NAT GEO WILD
108|US: DISNEY CHANNEL HD
111|GO: HALLMARK CHANNEL
114|GO: FAMILY MOVIE CLASSICS
117|US: TV ONE HD
120|US: FOX BUSINESS NETWORK HD
123|US: CNN HD
126|US: MSNBC HD
129|US: EVERYBODY LOVES RAYMOND 4K
132|24/7: MASH
135|24/7: THE BIG BANG THEORY
138|US: I LOVE LUCY 4K
141|US: THE ANDY GRIFFITH SHOW 4K
144|24/7: MODERN FAMILY
147|24/7: BRADY BUNCH
150|24/7: HOME IMPROVEMENT
153|24/7: LITTLE HOUSE ON THE PRAIRIE
156|24/7: LEAVE IT TO BEAVER
159|24/7: PETTICOAT JUNCTION
162|24/7: SEINFELD
165|24/7: THAT GIRL
168|24/7: THE ANDY GRIFFITH SHOW
171|24/7: THE THREE STOOGES
174|24/7: ACCORDING TO JIM
177|24/7: BETTER CALL SAUL
180|24/7: CAROL'S SECOND ACT
183|24/7: HOW ITS MADE
186|24/7: SCRUBS
189|24/7: THE OFFICE
192|24/7: TWO AND A HALF MEN
195|24/7: YOUNG SHELDON
198|US: 24/7 CLINT EASTWOOD
201|US: 24/7 WESTERN 1
204|US: 24/7 WESTERN 2
```

### Sample Xtream provider filter list

Paste into the **Xtream Channel Filter** field (`item.xtream_includes` — wildcard patterns, one per line):

```
*Fox News*
*CNN*
*MSNBC*
*Mash*
*ANTENNA TV*
*ESPN HD*
*METV*
*US: GRIT*
*US: ESPN 2 HD*
*US: NFL REDZONE*
*US: NFL NETWORK HD*
*US: MLB NETWORK*
*SLING: MLB NETWORK*
*SLING: NHL NETWORK*
*PRIME: WPXI PITTSBURGH (NBC)*
*US: FOX 53 (WPGH) PITTSBURGH HD*
*GO: METV*
*US: ANTENNA TV*
*US: CNN HD*
*US: DIY HD*
*PRIME: SUNDANCE TV*
*SLING: PARAMOUNT NETWORK*
*US: USA NETWORK 4K*
*US: TNT HD*
*US: TBS HD*
*US: COMEDY CENTRAL HD*
*US: EVERYBODY LOVES RAYMOND 4K*
*US: I LOVE LUCY 4K*
*24/7: HOME IMPROVEMENT*
*24/7: LITTLE HOUSE ON THE PRAIRIE*
*24/7: DEADLIEST CATCH*
*24/7: PAWN STARS*
*US: TCM HD*
*GO: FX MOVIE CHANNEL*
*GO: NATIONAL GEOGRAPHIC CHANNEL*
*US: TRAVEL CHANNEL HD*
*US: DISCOVERY HD*
*GO: TLC*
*US: MOTORTREND HD*
*GO: NAT GEO WILD*
*US: DISNEY CHANNEL HD*
*US: TV LAND HD*
*SLING: NEWSNATION*
*GO: HALLMARK CHANNEL*
*GO: FAMILY MOVIE CLASSICS*
*US: TV ONE HD*
*US: NEWSMAX HD*
*US: MSNBC HD*
*US: FOX BUSINESS NETWORK HD*
*US: FOX NEWS HD*
*US: THE WEATHER CHANNEL HD*
*US: HBO HD*
*US: HBO 2 HD*
*SLING: HBO DRAMA*
*US: HBO COMEDY HD*
*US: FX MOVIES*
*US: STARZ HD*
*US: STARZ COMEDY*
*US: STARZ CINEMA*
*US: STARZ ENCORE*
*US: STARZ ENCORE WESTERNS HD*
*SLING: SHOWTIME EXTREME*
*US: THE MOVIE CHANNEL HD*
*US: CINEMAX HD*
*US: SMITHSONIAN CHANNEL HD*
*US: AT&T SPORTSNET PITTSBURGH*
*SLING: PITTSBURGH PIRATES ᴿᴬᵂ*
*SLING: MLB STRIKE ZONE ᴿᴬᵂ*
*SLING: PARAMOUNT NETWORK ᴿᴬᵂ*
*24/7: MASH*
*24/7: THE BIG BANG THEORY*
*US: THE ANDY GRIFFITH SHOW 4K*
*24/7: MODERN FAMILY*
*24/7: BRADY BUNCH*
*24/7: LEAVE IT TO BEAVER*
*24/7: SEINFELD*
*24/7: THE ANDY GRIFFITH SHOW*
*24/7: THE THREE STOOGES*
*24/7: ACCORDING TO JIM*
*24/7: BETTER CALL SAUL*
*24/7: HOW ITS MADE*
*24/7: SCRUBS*
*24/7: THE OFFICE*
*24/7: TWO AND A HALF MEN*
*24/7: YOUNG SHELDON*
*US: 24/7 CLINT EASTWOOD*
```
