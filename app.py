#!/usr/bin/env python3
"""
SleepyTV - a local "TV Guide" for the iptv-org public IPTV collection.

Run:
    python app.py
then open http://localhost:8080 in your browser.

Zero external dependencies (Python standard library only).

What it does:
  * Fetches iptv-org's structured JSON API server-side (channels, streams,
    logos, categories, countries), merges it into one clean channel list,
    and caches it to disk so restarts are instant.
  * Serves a browser UI (index.html) with search, category/country filters,
    an "On Now / Hot" spotlight, and category rails.
  * Proxies the live HLS streams (/proxy) so far more of them actually play
    in the browser -- this sidesteps the CORS restrictions that otherwise
    make most IPTV streams fail, and forwards the User-Agent / Referer that
    some streams require.

Note: iptv-org aggregates publicly-available, free-to-air streams. Many are
flaky, geo-restricted, or offline at any given moment -- that's inherent to
the source, not a bug in this app.
"""

import os
import re
import ssl
import json
import gzip
import time
import calendar
import datetime
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
HOST = "0.0.0.0"
PORT = int(os.environ.get("SLEEPYTV_PORT", "8080"))
HERE = os.path.dirname(os.path.abspath(__file__))
# cache dir is configurable so a container can persist it on a mounted volume
CACHE_DIR = os.environ.get("SLEEPYTV_CACHE_DIR", HERE)
CACHE_FILE = os.path.join(CACHE_DIR, "data_cache.json")
CACHE_TTL = 12 * 3600  # refresh the channel list at most twice a day
EPG_CACHE_FILE = os.path.join(CACHE_DIR, "epg_cache.json")
EPG_TTL = 3 * 3600     # program schedules change through the day
HEALTH_CACHE_FILE = os.path.join(CACHE_DIR, "health_cache.json")
HEALTH_TTL = 6 * 3600  # re-probe which streams are alive every few hours
HEALTH_ENABLED = os.environ.get("SLEEPYTV_HEALTHCHECK", "1").lower() not in ("0", "false", "no")
API = "https://iptv-org.github.io/api"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# SSL context that doesn't verify certs -- lots of IPTV origins have broken
# or self-signed certs; this is a personal local tool so we favour "it plays".
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# --------------------------------------------------------------------------
# Data loading / merging
# --------------------------------------------------------------------------
DATA_BYTES = None          # gzipped JSON payload for /api/data
DATA_READY = threading.Event()
DATA_OBJ = None            # raw merged data (for M3U/XMLTV generation)
EPG_BYTES = None           # gzipped JSON payload for /api/epg
EPG_READY = threading.Event()
EPG_OBJ = None             # raw {channel_id: [[s, e, title], ...]}
HEALTH = {}                # stream url -> bool (reachable from this server)
HEALTH_TS = 0              # unix time of the last completed scan
HEALTH_SCANNING = False    # True while a probe pass is actively running
HEALTH_LOCK = threading.Lock()
HEALTH_READY = threading.Event()  # set after the first (priority) batch is probed


def _fetch_json(name):
    url = "%s/%s.json" % (API, name)
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def _quality_rank(q):
    if not q:
        return 0
    m = re.search(r"(\d{3,4})", str(q))
    return int(m.group(1)) if m else 0


def build_data():
    """Fetch + merge the iptv-org API into the compact shape the UI wants."""
    print("[data] fetching iptv-org API (channels, streams, logos, ...)")
    channels = _fetch_json("channels")
    streams = _fetch_json("streams")
    logos = _fetch_json("logos")
    categories = _fetch_json("categories")
    countries = _fetch_json("countries")
    regions = _fetch_json("regions")

    ch_by_id = {c["id"]: c for c in channels}

    # country -> continent -> Opera-VPN region bucket. Opera's free built-in VPN
    # only offers Europe / Americas / Asia, so we bucket each channel's home
    # region into one of those to help users pick the right VPN location for
    # geo-locked streams.
    CONT_PRIORITY = ["AMER", "EUR", "ASIA", "AFR", "OCE"]
    CONT_NAME = {"AMER": "Americas", "EUR": "Europe", "ASIA": "Asia",
                 "AFR": "Africa", "OCE": "Oceania"}
    VPN_BUCKET = {"AMER": "Americas", "EUR": "Europe", "AFR": "Europe",
                  "ASIA": "Asia", "OCE": "Asia"}
    reg_countries = {r["code"]: set(r.get("countries", []))
                     for r in regions if r["code"] in CONT_PRIORITY}

    def continent_of(cc):
        for code in CONT_PRIORITY:
            if cc in reg_countries.get(code, ()):
                return code
        return None

    # one logo per channel, preferring the ones marked in_use
    logo_by_ch = {}
    for lg in logos:
        cid = lg.get("channel")
        url = lg.get("url")
        if not cid or not url:
            continue
        if cid not in logo_by_ch or lg.get("in_use"):
            logo_by_ch[cid] = url

    country_name = {c["code"]: c["name"] for c in countries}

    items = {}      # keyed for dedupe
    standalone = []  # streams with no channel record
    for s in streams:
        url = s.get("url")
        if not url:
            continue
        cid = s.get("channel")
        ch = ch_by_id.get(cid) if cid else None

        cats = ch.get("categories") or [] if ch else []
        nsfw = bool(ch and (ch.get("is_nsfw") or "xxx" in cats))

        item = {
            "id": cid,   # iptv-org channel id (None for standalone streams) -- used to join EPG
            "name": s.get("title") or (ch.get("name") if ch else None) or cid or "Unknown",
            "url": url,
            "logo": logo_by_ch.get(cid) if cid else None,
            "cats": cats,
            "cc": (ch.get("country") if ch else None) or None,
            "q": s.get("quality") or "",
            "ua": s.get("user_agent") or "",
            "ref": s.get("referrer") or "",
            "nsfw": nsfw,
        }
        if cid:
            # dedupe: keep the highest-quality stream per channel
            prev = items.get(cid)
            if prev is None or _quality_rank(item["q"]) > _quality_rank(prev["q"]):
                items[cid] = item
        else:
            standalone.append(item)

    merged = list(items.values()) + standalone

    # only surface countries/categories that actually appear
    present_cc = sorted({i["cc"] for i in merged if i["cc"]},
                        key=lambda c: country_name.get(c, c))
    cc_list = []
    for c in present_cc:
        cont = continent_of(c)
        cc_list.append({"code": c, "name": country_name.get(c, c),
                        "cont": CONT_NAME.get(cont), "vpn": VPN_BUCKET.get(cont)})
    present_cats = sorted({c for i in merged for c in i["cats"]})
    cat_list = [{"id": c["id"], "name": c["name"]}
                for c in categories if c["id"] in present_cats]

    print("[data] merged %d playable channels (%d countries, %d categories)"
          % (len(merged), len(cc_list), len(cat_list)))
    return {"channels": merged, "countries": cc_list, "categories": cat_list,
            "generated": int(time.time())}


def load_data_cached():
    if os.path.exists(CACHE_FILE):
        age = time.time() - os.path.getmtime(CACHE_FILE)
        if age < CACHE_TTL:
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                print("[data] using cache (%.0f min old, %d channels)"
                      % (age / 60, len(data["channels"])))
                return data
            except Exception as e:
                print("[data] cache unreadable (%s), refetching" % e)
    data = build_data()
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        print("[data] could not write cache: %s" % e)
    return data


# --------------------------------------------------------------------------
# EPG (Electronic Program Guide)
# --------------------------------------------------------------------------
# iptv-org itself doesn't serve program data, but its guides.json maps channels
# to EPG sources. Two of those sources are *pre-generated hosted XMLTV* (no
# scraping needed): i.mjh.nz (the big FAST providers -- Plex/Pluto/Roku/Samsung)
# and epgshare01.online. We fetch only the files our channels actually need,
# parse the schedules, and key them by iptv-org channel id.
HOSTED_EPG = {"i.mjh.nz", "epgshare01.online"}
EPG_WINDOW_BACK = 3 * 3600      # keep a little history for "just started" shows
EPG_WINDOW_FWD = 30 * 3600      # ~a day and a bit ahead
EPG_MAX_PER_CH = 40


def _epg_file_url(site, path):
    if site == "i.mjh.nz":
        return "https://i.mjh.nz/%s.xml.gz" % path
    if site == "epgshare01.online":
        return "https://epgshare01.online/epgshare01/epg_ripper_%s.xml.gz" % path
    return None


def _parse_xmltv_time(s):
    """'20260711144222 +0000' (or without offset) -> unix epoch seconds."""
    if not s:
        return None
    s = s.strip()
    tz = 0
    if " " in s:
        base, off = s.split(" ", 1)
        off = off.strip()
        if len(off) >= 5 and off[0] in "+-":
            sign = 1 if off[0] == "+" else -1
            tz = sign * (int(off[1:3]) * 3600 + int(off[3:5]) * 60)
    else:
        base = s
    if len(base) < 12:
        return None
    try:
        dt = datetime.datetime(int(base[0:4]), int(base[4:6]), int(base[6:8]),
                               int(base[8:10]), int(base[10:12]),
                               int(base[12:14]) if len(base) >= 14 else 0)
        return calendar.timegm(dt.timetuple()) - tz
    except ValueError:
        return None


def build_epg(needed_ids):
    """Return {channel_id: [[start, stop, title], ...]} for the channels we have."""
    guides = _fetch_json("guides")

    # channel_id -> list of (site, path, xmltv_id); only for channels we display
    ch_sources = defaultdict(list)
    for e in guides:
        cid = e.get("channel")
        sid = e.get("site_id") or ""
        if (e.get("site") in HOSTED_EPG and cid in needed_ids and "#" in sid):
            path, xid = sid.split("#", 1)
            ch_sources[cid].append((e["site"], path, xid))

    files = sorted({(site, path) for lst in ch_sources.values()
                    for (site, path, _) in lst})
    wanted = defaultdict(set)  # (site, path) -> set of xmltv ids we care about
    for lst in ch_sources.values():
        for site, path, xid in lst:
            wanted[(site, path)].add(xid)

    now = time.time()
    lo, hi = now - EPG_WINDOW_BACK, now + EPG_WINDOW_FWD
    print("[epg] fetching %d guide files for %d channels..."
          % (len(files), len(ch_sources)))

    def load_one(site_path):
        site, path = site_path
        url = _epg_file_url(site, path)
        keep = wanted[site_path]
        out = defaultdict(list)   # xmltv_id -> [[s, e, title], ...]
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r:
                raw = r.read()
            xml = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
            root = ET.fromstring(xml)
            for p in root.iter("programme"):
                cid = p.get("channel")
                if cid not in keep:
                    continue
                s = _parse_xmltv_time(p.get("start"))
                e = _parse_xmltv_time(p.get("stop"))
                if s is None or e is None or e < lo or s > hi:
                    continue
                t = p.findtext("title") or ""
                out[cid].append([int(s), int(e), t.strip()])
        except Exception as ex:
            print("[epg]   skip %s (%s)" % (path, ex))
        return site_path, out

    file_progs = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for site_path, out in pool.map(load_one, files):
            file_progs[site_path] = out

    epg = {}
    for cid, lst in ch_sources.items():
        merged = []
        for site, path, xid in lst:
            merged.extend(file_progs.get((site, path), {}).get(xid, []))
        if not merged:
            continue
        # dedupe by start time, sort chronologically, cap
        seen, uniq = set(), []
        for p in sorted(merged, key=lambda x: x[0]):
            if p[0] in seen:
                continue
            seen.add(p[0])
            uniq.append(p)
        epg[cid] = uniq[:EPG_MAX_PER_CH]

    total = sum(len(v) for v in epg.values())
    print("[epg] built guide for %d channels (%d programmes)" % (len(epg), total))
    return {"epg": epg, "generated": int(now)}


def load_epg_cached(needed_ids):
    if os.path.exists(EPG_CACHE_FILE):
        age = time.time() - os.path.getmtime(EPG_CACHE_FILE)
        if age < EPG_TTL:
            try:
                with open(EPG_CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                print("[epg] using cache (%.0f min old, %d channels)"
                      % (age / 60, len(data.get("epg", {}))))
                return data
            except Exception as e:
                print("[epg] cache unreadable (%s), rebuilding" % e)
    data = build_epg(needed_ids)
    try:
        with open(EPG_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        print("[epg] could not write cache: %s" % e)
    return data


def warm_data():
    global DATA_BYTES, DATA_OBJ, EPG_BYTES, EPG_OBJ
    # Phase 1: channel list -- serve the UI as soon as this is ready.
    data = None
    try:
        data = load_data_cached()
        DATA_OBJ = data
        DATA_BYTES = gzip.compress(json.dumps(data).encode("utf-8"))
    except Exception as e:
        print("[data] FAILED to load channel data: %s" % e)
        err = json.dumps({"error": str(e), "channels": [],
                          "countries": [], "categories": []})
        DATA_BYTES = gzip.compress(err.encode("utf-8"))
    finally:
        DATA_READY.set()

    # Phase 2: EPG -- fills in "now/next" and the Guide grid once ready.
    try:
        needed = {i["id"] for i in (data or {}).get("channels", []) if i.get("id")}
        epg = load_epg_cached(needed) if needed else {"epg": {}, "generated": 0}
        EPG_OBJ = epg.get("epg", {})
        EPG_BYTES = gzip.compress(json.dumps(epg).encode("utf-8"))
    except Exception as e:
        print("[epg] FAILED to build guide: %s" % e)
        EPG_OBJ = {}
        EPG_BYTES = gzip.compress(b'{"epg":{},"generated":0}')
    finally:
        EPG_READY.set()


# --------------------------------------------------------------------------
# M3U + XMLTV generation (so Jellyfin / Plex / any IPTV client can use SleepyTV
# as a Live-TV backend: point an "M3U Tuner" at /playlist.m3u and an "XMLTV"
# guide provider at /epg.xml).
# --------------------------------------------------------------------------
def _cat_name_map():
    return {c["id"]: c["name"] for c in (DATA_OBJ or {}).get("categories", [])}


def filter_channels(qs):
    """Apply ?cat=&country=&epg=&nsfw= filters to the merged channel list."""
    items = (DATA_OBJ or {}).get("channels", [])
    cats = set(filter(None, (qs.get("cat", [""])[0]).split(",")))
    countries = set(filter(None, (qs.get("country", [""])[0]).split(",")))
    epg_only = (qs.get("epg", ["0"])[0]) in ("1", "true", "yes")
    show_nsfw = (qs.get("nsfw", ["0"])[0]) in ("1", "true", "yes")
    working_only = (qs.get("working", ["0"])[0]) in ("1", "true", "yes")
    out = []
    for it in items:
        if it.get("nsfw") and not show_nsfw:
            continue
        if working_only and not HEALTH.get(it["url"]):
            continue
        if epg_only and not (it.get("id") and it["id"] in (EPG_OBJ or {})):
            continue
        if cats and not (set(it.get("cats", [])) & cats):
            continue
        if countries and it.get("cc") not in countries:
            continue
        out.append(it)
    return out


def _m3u_attr(s):
    return (s or "").replace('"', "'").replace("\n", " ").replace("\r", "").strip()


def build_m3u(items, host, use_proxy):
    cat_names = _cat_name_map()
    lines = ['#EXTM3U url-tvg="/epg.xml"']
    for it in items:
        name = _m3u_attr(it["name"])
        group = "Live TV"
        if it.get("cats"):
            group = cat_names.get(it["cats"][0], it["cats"][0].title())
        elif it.get("cc"):
            group = it["cc"]
        ext = "#EXTINF:-1"
        if it.get("id"):
            ext += ' tvg-id="%s"' % _m3u_attr(it["id"])
        if it.get("logo"):
            ext += ' tvg-logo="%s"' % _m3u_attr(it["logo"])
        ext += ' group-title="%s",%s' % (_m3u_attr(group), name)
        lines.append(ext)
        if it.get("ua"):
            lines.append("#EXTVLCOPT:http-user-agent=%s" % it["ua"])
        if it.get("ref"):
            lines.append("#EXTVLCOPT:http-referrer=%s" % it["ref"])
        url = it["url"]
        if use_proxy and host:
            q = {"url": url}
            if it.get("ua"):
                q["ua"] = it["ua"]
            if it.get("ref"):
                q["ref"] = it["ref"]
            url = "http://%s/proxy?%s" % (host, urllib.parse.urlencode(q))
        lines.append(url)
    return "\n".join(lines) + "\n"


def _xml_escape(s):
    return ((s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _xmltv_time(epoch):
    return time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(epoch))


def build_xmltv():
    epg = EPG_OBJ or {}
    items_by_id = {i["id"]: i for i in (DATA_OBJ or {}).get("channels", []) if i.get("id")}
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<tv generator-info-name="SleepyTV">']
    for cid in epg:
        it = items_by_id.get(cid)
        name = _xml_escape(it["name"] if it else cid)
        icon = ('<icon src="%s" />' % _xml_escape(it["logo"])) if (it and it.get("logo")) else ""
        out.append('<channel id="%s"><display-name>%s</display-name>%s</channel>'
                   % (_xml_escape(cid), name, icon))
    for cid, progs in epg.items():
        for p in progs:
            out.append('<programme start="%s" stop="%s" channel="%s"><title>%s</title></programme>'
                       % (_xmltv_time(p[0]), _xmltv_time(p[1]), _xml_escape(cid),
                          _xml_escape(p[2])))
    out.append('</tv>')
    return "\n".join(out)


# --------------------------------------------------------------------------
# Stream health checks
# --------------------------------------------------------------------------
# Probe each stream *from this server* so "working" means "reachable from where
# Jellyfin/your browser will actually fetch it" (catches dead links, 403 geo
# blocks, DNS failures, timeouts). Results power a "known working" filter in the
# UI and the ?working=1 option on /playlist.m3u.
def check_stream(url, ua, ref):
    headers = {"User-Agent": ua or BROWSER_UA}
    if ref:
        headers["Referer"] = ref
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8, context=SSL_CTX) as r:
            if (getattr(r, "status", 200) or 200) >= 400:
                return False
            head = r.read(1024)
    except Exception:
        return False
    low = url.lower().split("?")[0]
    if ".m3u8" in low or head[:7] == b"#EXTM3U":
        return b"#EXT" in head          # a real HLS playlist
    return len(head) > 0                 # some other media that responded


def save_health():
    try:
        ok = [u for u, v in HEALTH.items() if v]
        with open(HEALTH_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"ts": int(time.time()), "ok": ok}, f)
    except Exception as e:
        print("[health] could not write cache: %s" % e)


def load_health_cache():
    global HEALTH_TS
    if not os.path.exists(HEALTH_CACHE_FILE):
        return False
    try:
        with open(HEALTH_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        with HEALTH_LOCK:
            for u in data.get("ok", []):
                HEALTH[u] = True
        HEALTH_TS = data.get("ts", 0)
        print("[health] loaded %d known-working streams from cache" % len(HEALTH))
        HEALTH_READY.set()
        return time.time() - HEALTH_TS < HEALTH_TTL
    except Exception as e:
        print("[health] cache unreadable (%s)" % e)
        return False


def _probe_batch(items):
    def work(it):
        return it["url"], check_stream(it["url"], it.get("ua"), it.get("ref"))
    with ThreadPoolExecutor(max_workers=25) as pool:
        for url, ok in pool.map(work, items):
            with HEALTH_LOCK:
                HEALTH[url] = ok


def health_scan():
    global HEALTH_SCANNING
    HEALTH_SCANNING = True
    try:
        items = (DATA_OBJ or {}).get("channels", [])
        epg = EPG_OBJ or {}
        # probe the guide-backed channels first (the premium lineup), then the rest
        priority = [it for it in items if it.get("id") and it["id"] in epg]
        rest = [it for it in items if not (it.get("id") and it["id"] in epg)]
        print("[health] probing %d streams (%d guide-backed first)..."
              % (len(items), len(priority)))
        _probe_batch(priority)
        HEALTH_READY.set()
        save_health()
        _probe_batch(rest)
        save_health()
        with HEALTH_LOCK:
            working = sum(1 for v in HEALTH.values() if v)
        print("[health] scan done: %d/%d streams reachable" % (working, len(HEALTH)))
    finally:
        HEALTH_SCANNING = False


def health_worker():
    if not HEALTH_ENABLED:
        HEALTH_READY.set()
        print("[health] disabled (SLEEPYTV_HEALTHCHECK=0)")
        return
    global HEALTH_TS
    fresh = load_health_cache()
    DATA_READY.wait()
    EPG_READY.wait()
    while True:
        if not fresh:
            try:
                health_scan()
                HEALTH_TS = time.time()
            except Exception as e:
                print("[health] scan error: %s" % e)
        fresh = False
        time.sleep(HEALTH_TTL)


# --------------------------------------------------------------------------
# HLS stream proxy
# --------------------------------------------------------------------------
def _proxify(url, ua, ref):
    q = {"url": url}
    if ua:
        q["ua"] = ua
    if ref:
        q["ref"] = ref
    return "/proxy?" + urllib.parse.urlencode(q)


_URI_ATTR = re.compile(r'URI="([^"]+)"')


def _rewrite_m3u8(text, base_url, ua, ref):
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
        elif s.startswith("#"):
            if "URI=" in s:
                def repl(m):
                    resolved = urllib.parse.urljoin(base_url, m.group(1))
                    return 'URI="%s"' % _proxify(resolved, ua, ref)
                out.append(_URI_ATTR.sub(repl, line))
            else:
                out.append(line)
        else:
            out.append(_proxify(urllib.parse.urljoin(base_url, s), ua, ref))
    return "\n".join(out)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quieter console
        pass

    # -- helpers ----------------------------------------------------------
    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8",
              extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    # -- routing ----------------------------------------------------------
    def do_HEAD(self):
        # Some clients (incl. Jellyfin's tuner/guide validation) probe with HEAD.
        # Reuse the GET routing; _send and the proxy skip the body for HEAD.
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            return self._serve_index()
        if path == "/api/data":
            return self._serve_data()
        if path == "/api/epg":
            return self._serve_epg()
        if path == "/api/health":
            return self._serve_health_data()
        if path == "/playlist.m3u":
            return self._serve_m3u(urllib.parse.parse_qs(parsed.query))
        if path == "/epg.xml":
            return self._serve_xmltv()
        if path == "/healthz":
            return self._serve_health()
        if path == "/proxy":
            return self._serve_proxy(urllib.parse.parse_qs(parsed.query))
        if path == "/favicon.ico":
            return self._send(204)
        return self._send(404, b"Not found")

    def _serve_index(self):
        try:
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                body = f.read()
            self._send(200, body, "text/html; charset=utf-8")
        except FileNotFoundError:
            self._send(500, b"index.html missing next to app.py")

    def _serve_data(self):
        DATA_READY.wait()
        self._serve_gzip_json(DATA_BYTES)

    def _serve_epg(self):
        EPG_READY.wait()
        self._serve_gzip_json(EPG_BYTES)

    def _serve_health(self):
        # 200 once the channel list is loaded (container is usable), so Dockge /
        # Docker report "healthy". EPG readiness is reported in the body.
        ready = DATA_READY.is_set()
        body = json.dumps({
            "status": "ok" if ready else "starting",
            "channels": len((DATA_OBJ or {}).get("channels", [])) if ready else 0,
            "epg_ready": EPG_READY.is_set(),
            "epg_channels": len(EPG_OBJ or {}),
        }).encode("utf-8")
        self._send(200 if ready else 503, body, "application/json; charset=utf-8")

    def _serve_health_data(self):
        DATA_READY.wait()
        with HEALTH_LOCK:
            ok = [u for u, v in HEALTH.items() if v]
            checked = len(HEALTH)
        body = json.dumps({
            "ok": ok,
            "working": len(ok),
            "checked": checked,
            "scanning": HEALTH_SCANNING,
            "enabled": HEALTH_ENABLED,
            "ts": HEALTH_TS,
        }).encode("utf-8")
        self._serve_gzip_json(gzip.compress(body))

    def _serve_m3u(self, qs):
        DATA_READY.wait()
        EPG_READY.wait()  # so ?epg=1 filtering works on first hit
        if (qs.get("working", ["0"])[0]) in ("1", "true", "yes"):
            HEALTH_READY.wait(timeout=120)  # first working request waits for the priority scan
        use_proxy = (qs.get("proxy", ["0"])[0]) in ("1", "true", "yes")
        host = self.headers.get("Host")
        items = filter_channels(qs)
        body = build_m3u(items, host, use_proxy).encode("utf-8")
        self._send(200, body, "application/x-mpegurl; charset=utf-8",
                   {"Content-Disposition": 'inline; filename="sleepytv.m3u"'})

    def _serve_xmltv(self):
        EPG_READY.wait()
        body = build_xmltv().encode("utf-8")
        self._send(200, body, "application/xml; charset=utf-8",
                   {"Content-Disposition": 'inline; filename="sleepytv-epg.xml"'})

    def _serve_gzip_json(self, blob):
        accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "")
        body = blob
        extra = {"Cache-Control": "no-cache"}
        if accepts_gzip:
            extra["Content-Encoding"] = "gzip"
        else:
            body = gzip.decompress(blob)
        self._send(200, body, "application/json; charset=utf-8", extra)

    def _serve_proxy(self, qs):
        target = (qs.get("url") or [None])[0]
        if not target:
            return self._send(400, b"missing url")
        ua = (qs.get("ua") or [""])[0] or BROWSER_UA
        ref = (qs.get("ref") or [""])[0]

        headers = {"User-Agent": ua}
        if ref:
            headers["Referer"] = ref
        client_range = self.headers.get("Range")
        if client_range:
            headers["Range"] = client_range

        try:
            req = urllib.request.Request(target, headers=headers)
            upstream = urllib.request.urlopen(req, timeout=20, context=SSL_CTX)
        except Exception as e:
            return self._send(502, ("upstream error: %s" % e).encode())

        try:
            final_url = upstream.geturl()
            ctype = upstream.headers.get("Content-Type", "")
            path_l = urllib.parse.urlparse(final_url).path.lower()

            # peek to decide: playlist (rewrite) vs. media segment (stream)
            head = upstream.read(8192)
            is_m3u8 = (path_l.endswith(".m3u8") or "mpegurl" in ctype.lower()
                       or head[:7] == b"#EXTM3U")

            if is_m3u8:
                body = head + upstream.read()
                text = body.decode("utf-8", "replace")
                rewritten = _rewrite_m3u8(text, final_url, ua, ref)
                self._send(200, rewritten.encode("utf-8"),
                           "application/vnd.apple.mpegurl",
                           {"Cache-Control": "no-cache"})
                return

            # media segment / key: stream it through
            status = getattr(upstream, "status", 200) or 200
            self.send_response(status)
            out_ctype = ctype or "application/octet-stream"
            self.send_header("Content-Type", out_ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            clen = upstream.headers.get("Content-Length")
            if clen:
                self.send_header("Content-Length", clen)
            crange = upstream.headers.get("Content-Range")
            if crange:
                self.send_header("Content-Range", crange)
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if self.command == "HEAD":
                return
            self.wfile.write(head)
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client closed the player; normal
        except Exception as e:
            try:
                self._send(502, ("proxy error: %s" % e).encode())
            except Exception:
                pass
        finally:
            upstream.close()


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    threading.Thread(target=warm_data, daemon=True).start()
    threading.Thread(target=health_worker, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = "http://localhost:%d" % PORT
    print("=" * 56)
    print(" SleepyTV is running -> %s" % url)
    print(" Live-TV backend for Jellyfin/Plex/etc:")
    print("   M3U tuner : %s/playlist.m3u  (try ?epg=1 or ?working=1)" % url)
    print("   XMLTV EPG : %s/epg.xml" % url)
    print(" (loading channel data in the background...)")
    print(" Press Ctrl+C to stop.")
    print("=" * 56)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")
        server.shutdown()


if __name__ == "__main__":
    main()
