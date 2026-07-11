# 🌙 SleepyTV

A local "TV Guide" for the free, publicly-available live TV streams collected by
[iptv-org](https://github.com/iptv-org/iptv). Browse, search, and watch ~12,500
live channels in your browser — with a real **Electronic Program Guide (EPG)**
where schedule data is available. Runs entirely on your machine with **zero
dependencies** — just Python.

![runs on: python 3 stdlib](https://img.shields.io/badge/python-3.x%20stdlib-blue)

## Run it

```bash
python app.py
```

Then open **http://localhost:8080**.

That's it — no `pip install`, no accounts, no build step. (Requires Python 3.7+.)
The channel list appears in a few seconds; the program guide fills in a little
after that (it fetches and parses the schedule files in the background).

## What you get

- **🧭 Discover view**
  - **On Now spotlight** — a rotating live channel (hit *Shuffle* for another).
  - **⭐ Showcase rail** — hand-picked marquee free channels (NASA, Red Bull TV,
    Al Jazeera, DW, France 24, Euronews, Bloomberg…) that reliably play — a
    strong first impression.
  - **Category rails** — Hot News, Sports, Movies, Music, Kids, Documentary,
    Entertainment.
  - Every card shows **▶ what's on now** + a progress bar + **Next:** when the
    channel has guide data.
- **📅 Guide view** — the classic EPG grid: channels down the left, a time axis
  across the top, program blocks, and a live **NOW** line. Click any programme to
  watch that channel. (~940 channels have published schedules.)
- **Search** — type any channel name.
- **Filters** — by category and by country (177 countries, with flags).
- **🔞 18+ toggle** — off by default. Turn it on to include adult channels.
- **Player** — plays in-page, and shows **Now / Up-next** for guide channels.

## The EPG (program guide)

iptv-org itself doesn't serve program data, but its `guides.json` maps channels to
EPG sources. SleepyTV uses the two that are **pre-generated hosted XMLTV** (no
scraping): [i.mjh.nz](https://i.mjh.nz) (the big FAST providers — **Plex, Pluto,
Roku, Samsung TV Plus**) and epgshare01.online. It fetches only the guide files
your channels need, parses the schedules, and caches them.

**Coverage is honest:** roughly **940 of the ~12,500 channels** have real
schedules — and, conveniently, those are the FAST-provider channels that are also
the most reliable to play. Every other channel is genuine 24/7 live TV and simply
shows a **LIVE** badge (no per-program schedule exists for it without scraping).

## About "all channels, including NSFW"

Adult channels are included (no filtering). In practice the public iptv-org set
contains **very few** working adult streams — it catalogs ~375 adult channels but
only a handful have a live stream entry (the rest are removed/blocklisted
upstream). Those that exist are hidden until you flip the **🔞 18+** toggle, and
your choice is remembered in the browser.

## Watching geo-locked channels (Opera's free VPN)

These are free, public streams, but many are **geo-locked to their home country**
— the single most common reason a stream won't play. The fix is simple: watch in
**[Opera](https://www.opera.com/)** and turn on its **free, built-in VPN**
(Settings → Privacy → VPN), which lets you appear in **Europe, the Americas, or
Asia**. Pick the region that matches the channel and it unlocks.

SleepyTV guides this for you:

- Every channel knows its home country, and the player shows a **region chip**
  (e.g. `📍 🇬🇧 United Kingdom · VPN: Europe`).
- When a stream fails, the player tells you **exactly which Opera VPN region to
  pick** and offers a **Try again** button — so you switch region and retry
  without guessing.

With the right region selected, the vast majority of the catalog becomes
watchable — a genuine showcase of how much free, live TV is out there.

## How it works

`app.py` is a small standard-library web server that:

1. **Merges the iptv-org JSON API** (channels, streams, logos, categories,
   countries) into one clean channel list, keeps the best-quality stream per
   channel, and caches it to `data_cache.json` (refreshed every 12h).
2. **Builds the EPG** from hosted XMLTV, keyed by channel id, cached to
   `epg_cache.json` (refreshed every 3h since schedules change through the day).
3. **Serves the UI** (`index.html`). Video playback uses
   [hls.js](https://github.com/video-dev/hls.js).
4. **Proxies the streams** (`/proxy`) — fetches each stream server-side, rewrites
   the HLS playlists, and forwards the `User-Agent`/`Referer` some streams need.
   This sidesteps the browser CORS restrictions that otherwise make most public
   IPTV refuse to play.

Delete `data_cache.json` / `epg_cache.json` any time to force a fresh pull.

### Endpoints

| Route | Purpose |
|-------|---------|
| `/` | The TV-guide UI |
| `/api/data` | Merged channel list (gzipped JSON) |
| `/api/epg` | Program schedules `{channelId: [[start, stop, title], …]}` |
| `/playlist.m3u` | **M3U tuner** for Jellyfin/Plex/etc. Filters: `?epg=1` `?cat=news,sports` `?country=US,UK` `?nsfw=1` `?proxy=1` |
| `/epg.xml` | **XMLTV guide** matching the playlist's `tvg-id`s |
| `/proxy?url=…` | CORS-friendly HLS proxy (rewrites playlists, streams segments) |
| `/healthz` | Health probe — `200` once channels are loaded (used by the container healthcheck) |

## Use as a Jellyfin Live-TV backend (Dockge on sleepycore)

SleepyTV doubles as a **Live-TV tuner + guide** for Jellyfin, so your local media
(from `\\sleepynas\PlexMediaServer`) and free live TV live behind **one** app.
Jellyfin pulls its channel list from `/playlist.m3u` and its guide from `/epg.xml`
— the `tvg-id`s and XMLTV channel ids match 1:1, so the guide binds cleanly.

This repo ships a ready Dockge stack: [`compose.yaml`](compose.yaml),
[`Dockerfile`](Dockerfile), [`.env.example`](.env.example).

### 1. Deploy the stack

On sleepycore, prerequisites: Docker + Dockge, **nvidia-container-toolkit**
(for NVENC), and **cifs-utils** (for the SMB mount).

1. Put this folder on sleepycore (git clone or copy) — it becomes a Dockge stack.
2. `cp .env.example .env` and fill in your NAS username/password (used only to
   mount the media share; **you** enter these, they aren't stored in the repo).
3. In Dockge, add the stack and **Deploy**. It builds `sleepytv` (no
   dependencies) and runs `jellyfin` with GPU passthrough.
   - SleepyTV → `http://sleepycore:8080` (browser UI + M3U/XMLTV) — add it to
     your dashboard/reverse proxy like your other apps.
   - Jellyfin → `http://sleepycore:8096`.

> The media share mounts read-only, so **Plex keeps working** — Jellyfin just
> reads the same files and builds its own metadata.

### 2. Point Jellyfin at SleepyTV

In Jellyfin → **Dashboard → Live TV**:

- **Tuner Devices → + → M3U Tuner**, URL:
  `http://sleepytv:8080/playlist.m3u?epg=1`
  *(`sleepytv` resolves on the compose network. `?epg=1` gives the ~940
  guide-backed channels — a clean lineup. Broaden with e.g.
  `?country=US,UK` or drop the query for all ~12,500 channels.)*
- **TV Guide Data Providers → + → XMLTV**, URL:
  `http://sleepytv:8080/epg.xml`
- **Refresh Guide**, then map channels if prompted.

### 3. Turn on NVENC transcoding

Jellyfin → **Dashboard → Playback → Transcoding** → Hardware acceleration =
**NVIDIA NVENC**, and enable the codecs your GPU supports. The `compose.yaml`
already grants the container the GPU.

### Notes for the Jellyfin path

- **Geo-blocking is server-side here.** Jellyfin fetches streams from sleepycore,
  so geo-locked channels depend on its IP — the Opera-VPN trick only helps in the
  **browser UI**, which is why we kept it. (A channel that won't play in Jellyfin
  may still play in the SleepyTV UI opened in Opera with the VPN on.)
- Streams that need special headers carry `#EXTVLCOPT:http-user-agent` /
  `http-referrer` in the M3U, which Jellyfin/ffmpeg respect. If a stubborn stream
  still won't play, add **`&proxy=1`** to the tuner URL to route it through
  SleepyTV's proxy.
- Country codes follow iptv-org's (mostly ISO 3166, but the UK is **`UK`**, not
  `GB`). Grab codes from the country dropdown in the SleepyTV UI.

### Dashboard tiles

The `compose.yaml` includes **Homepage** ([gethomepage.dev](https://gethomepage.dev))
auto-discovery labels for both services (harmlessly ignored if you don't use
Homepage). For a live Jellyfin widget, uncomment the `homepage.widget.*` lines and
add a Jellyfin API key.

**Homarr:** add each as a manual app —
- Jellyfin → `http://sleepycore:8096` (Homarr has a built-in Jellyfin integration
  for a live widget).
- SleepyTV → `http://sleepycore:8080`, ping URL `http://sleepycore:8080/healthz`
  (shows the tile online once the guide has loaded).

The **healthcheck** on `sleepytv` turns the container green in Dockge once the
channel list is loaded (~10s after start).

## Good to know

- These are **public, free-to-air** streams. Many are flaky, geo-restricted, or
  offline at any moment — that's normal for public IPTV, not a bug here. If one
  doesn't load, the player says so; try another.
- An internet connection is required (streams, channel list, and EPG are remote).
  hls.js loads from a CDN.
- Everything runs on `localhost`; nothing is sent anywhere except to fetch the
  public data and the streams you choose to play.
- To change the port, edit `PORT` near the top of `app.py`.

## Files

| File | What it is |
|------|------------|
| `app.py` | The server: data merge, EPG builder, UI hosting, stream proxy, M3U/XMLTV |
| `index.html` | The TV-guide web UI (Discover + Guide views, search, player) |
| `compose.yaml` | Dockge stack: SleepyTV + Jellyfin (NVIDIA) + SMB media mount |
| `Dockerfile` | Builds the SleepyTV container (no dependencies) |
| `.env.example` | Template for your NAS credentials (copy to `.env`) |
| `data_cache.json` | Auto-generated channel cache (safe to delete) |
| `epg_cache.json` | Auto-generated program-guide cache (safe to delete) |
| `.claude/launch.json` | Lets Claude Code launch the app in its preview pane |
