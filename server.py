#!/usr/bin/env python3
"""Orange Watch: a local open-source-intelligence feed aggregator.

Fetches the public RSS/Atom/JSON feeds listed in sources.json, normalizes
them into one list, and serves a dashboard at http://localhost:8787.
The Orange & OC tier refreshes about every minute and keeps every story for
7 days (cache/local_archive.json); the other tiers refresh hourly.

Standard library only. Usage:
    python3 server.py               # serve the dashboard (opens your browser)
    python3 server.py --no-browser  # serve without opening a browser
    python3 server.py --snapshot out.json   # fetch once, write JSON, exit
"""
import argparse
import concurrent.futures as cf
import email.utils
import hashlib
import html
import json
import os
import re
import threading
import time
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.json"
INDEX_FILE = ROOT / "index.html"
FETCH_TIMEOUT = 15
DEFAULT_MAX_ITEMS = 30
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X) OrangeWatch/1.0 (local OSINT reader)"
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


# ---------- helpers ----------------------------------------------------------

def load_config():
    return json.loads(SOURCES_FILE.read_text(encoding="utf-8"))


def fetch(url, ua=None):
    req = urllib.request.Request(url, headers={
        "User-Agent": ua or UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, "
                  "application/geo+json, application/json;q=0.9, */*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        return r.read()


def clean(text, limit=None):
    if not text:
        return ""
    text = html.unescape(TAG_RE.sub(" ", text))
    text = WS_RE.sub(" ", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def parse_date(s):
    """Return epoch seconds (int) or None."""
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d is not None:
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return int(d.timestamp())
    except (TypeError, ValueError, IndexError):
        pass
    try:
        iso = s.replace("Z", "+00:00")
        iso = re.sub(r"\.(\d{6})\d+", r".\1", iso)  # trim >6 fractional digits
        d = datetime.fromisoformat(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp())
    except ValueError:
        return None


def local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def child(el, *names):
    for c in el:
        if local(c.tag) in names:
            return c
    return None


def text_of(el, *names):
    c = child(el, *names)
    return (c.text or "").strip() if c is not None and c.text else ""


def make_item(src, title, link, ts, summary="", via=None, severity=None, extra=None):
    title = clean(title, 300)
    item = {
        "id": hashlib.sha1((link or title).encode("utf-8")).hexdigest()[:14],
        "title": title,
        "link": link,
        "ts": ts,
        "summary": clean(summary, 320),
        "source": src["id"],
        "tier": src["tier"],
        "kind": src.get("kind", "news"),
        "via": via,
        "severity": severity,
    }
    if extra:
        item.update(extra)
    return item


# ---------- parsers ----------------------------------------------------------

def parse_feed(src, raw):
    root = ET.fromstring(raw)
    items = []
    for el in root.iter():
        name = local(el.tag)
        if name == "item":           # RSS 2.0 and RDF/RSS 1.0
            title = text_of(el, "title")
            link = text_of(el, "link") or text_of(el, "guid")
            date = text_of(el, "pubDate", "date", "published", "updated")
            summary = text_of(el, "description", "encoded", "summary")
        elif name == "entry":        # Atom
            title = text_of(el, "title")
            link = ""
            for c in el:
                if local(c.tag) == "link" and c.get("rel", "alternate") == "alternate":
                    link = c.get("href", "")
                    break
            date = text_of(el, "published", "updated")
            summary = text_of(el, "summary", "content")
        else:
            continue
        via = None
        if src.get("gnews"):
            s = child(el, "source")
            if s is not None and s.text:
                via = s.text.strip()
                suffix = " - " + via
                if title.endswith(suffix):
                    title = title[: -len(suffix)]
            summary = ""  # Google News descriptions only repeat the headline
        # Drupal sites (ocgov.com, ocsheriff.gov) prefix the body with title, author and image name
        m = re.search(r"\bBody\s", clean(summary))
        if m and clean(summary).startswith(clean(title)[:30]):
            summary = clean(summary)[m.end():]
        severity = None
        lvl = text_of(el, "alertlevel")
        if lvl:
            severity = lvl.lower()   # GDACS: green / orange / red
        items.append(make_item(src, title, link, parse_date(date), summary, via, severity))
    return items


NWS_SEVERITY = {"Extreme": "red", "Severe": "red", "Moderate": "orange", "Minor": "yellow"}


def parse_nws(src, raw):
    data = json.loads(raw)
    wanted = src.get("filter") or []
    items = []
    for f in data.get("features", []):
        p = f.get("properties", {})
        area = p.get("areaDesc", "")
        if wanted and not any(w.lower() in area.lower() for w in wanted):
            continue
        title = f"{p.get('event', 'Alert')}: {area}"
        link = "https://alerts.weather.gov/search?id=" + p.get("id", "") if p.get("id") else src["home"]
        summary = p.get("headline") or p.get("description", "")
        items.append(make_item(src, title, link, parse_date(p.get("sent") or p.get("effective")),
                               summary, "National Weather Service",
                               NWS_SEVERITY.get(p.get("severity"), "yellow"),
                               {"expires": parse_date(p.get("ends") or p.get("expires"))}))
    return items


def parse_usgs(src, raw):
    data = json.loads(raw)
    items = []
    for f in data.get("features", []):
        p = f.get("properties", {})
        mag = p.get("mag")
        coords = (f.get("geometry") or {}).get("coordinates") or [None, None, None]
        sev = "red" if (mag or 0) >= 6 else "orange" if (mag or 0) >= 4.5 else "yellow" if (mag or 0) >= 3.5 else None
        title = p.get("title") or f"M{mag} {p.get('place', '')}"
        summary = f"Magnitude {mag}, depth {coords[2]:.1f} km. {p.get('place', '')}" if coords[2] is not None else p.get("place", "")
        items.append(make_item(src, title, p.get("url"), int(p["time"] / 1000) if p.get("time") else None,
                               summary, "USGS", sev,
                               {"mag": mag, "lat": coords[1], "lon": coords[0]}))
    return items


def parse_calfire(src, raw):
    data = json.loads(raw)
    items = []
    for inc in data if isinstance(data, list) else data.get("Incidents", []):
        name = inc.get("Name", "Incident")
        county = inc.get("County") or inc.get("Counties") or ""
        acres = inc.get("AcresBurned")
        contained = inc.get("PercentContained")
        bits = []
        if acres is not None:
            bits.append(f"{acres:,} acres" if isinstance(acres, (int, float)) else f"{acres} acres")
        if contained is not None:
            bits.append(f"{contained}% contained")
        if inc.get("Location"):
            bits.append(inc["Location"])
        url = inc.get("Url") or src["home"]
        if url.startswith("/"):
            url = "https://www.fire.ca.gov" + url
        sev = "red" if (contained is not None and contained < 50) else "orange"
        items.append(make_item(src, f"{name} ({county} County)" if county else name, url,
                               parse_date(inc.get("Updated") or inc.get("Started")),
                               " · ".join(bits), "CAL FIRE", sev,
                               {"lat": inc.get("Latitude"), "lon": inc.get("Longitude")}))
    return items


PARSERS = {"rss": parse_feed, "nws": parse_nws, "usgs": parse_usgs, "calfire": parse_calfire}


# ---------- Orange, CA relevance --------------------------------------------

def _rx(words):
    return re.compile(r"(?i)(?<![\w])(" + "|".join(words) + r")(?![\w])") if words else None


class LocalFilter:
    """Keeps local-tier stories about Orange, California and Orange County, CA.

    Rules come from the "local_filter" block in sources.json:
      - reject: text naming another state's Orange (Orange, Texas; West Orange; Orange County, Fla. ...)
      - noise: publishers and title patterns that are listings, obituaries, TV promos, etc.
      - strict sources: keep a story only if it names an OC place or California marker
      - city_of_orange: patterns that tag a story as being about the City of Orange itself
    """

    def __init__(self, cfg):
        f = cfg.get("local_filter", {})
        self.reject = _rx(f.get("reject_patterns", []))
        self.positive = _rx(f.get("oc_places", []))
        self.city = _rx(f.get("city_of_orange", []))
        self.noise_title = _rx(f.get("noise_title_patterns", []))
        self.noise_pub = {p.lower() for p in f.get("noise_publishers", [])}
        self.out_of_state_pub = {p.lower() for p in f.get("out_of_state_publishers", [])}
        self.trusted_pub = {p.lower() for p in f.get("trusted_publishers", [])}
        self.bare_orange = re.compile(r"(?<![\w])Orange(?![\w])")

    def check(self, src, item):
        """Return (keep, reason)."""
        text = item["title"] + " " + item["summary"]
        via = (item.get("via") or "").lower()
        if via and via in self.noise_pub:
            return False, "noise publisher"
        if via and via in self.out_of_state_pub:
            return False, "out-of-state publisher"
        if self.noise_title and self.noise_title.search(item["title"]):
            return False, "noise title"
        if self.reject and self.reject.search(text):
            return False, "names another state's Orange"
        if src.get("strict"):
            named = bool(self.positive and self.positive.search(text))
            # A bare "Orange" counts only from a SoCal outlet. Patch runs local sites in many
            # states, so it is trusted only on the Google News location feed for Orange, CA.
            trusted = via in self.trusted_pub and (via != "patch" or src.get("id") == "gn-geo-orange")
            if not named and not (trusted and self.bare_orange.search(text)):
                return False, "no Orange County, CA place named"
        return True, ""

    def is_city(self, src, item):
        if src.get("city_of_orange"):
            return True
        return bool(self.city and self.city.search(item["title"] + " " + item["summary"]))


# ---------- aggregation ------------------------------------------------------

ARCHIVE_FILE = ROOT / "cache" / "local_archive.json"
ARCHIVE_DAYS = 7
SCHED_TICK = 15  # seconds between scheduler checks


class Aggregator:
    """Each source refreshes on its own clock: the local tier every minute or so,
    everything else hourly (see refresh_seconds in sources.json). Local stories
    accumulate in an archive for ARCHIVE_DAYS so nothing drops off when a feed rotates."""

    def __init__(self):
        self.lock = threading.RLock()
        self.cfg = load_config()
        self.state = {}      # source id -> {"items", "status", "fetched"}
        self.archive = self._load_archive()
        self.filter_log = []  # recent rejected local stories, for the source-health panel
        self.version = 0     # bumps whenever any source's items change

    # -- archive persistence
    def _load_archive(self):
        try:
            data = json.loads(ARCHIVE_FILE.read_text(encoding="utf-8"))
            return {i["id"]: i for i in data}
        except Exception:
            return {}

    def _save_archive(self):
        try:
            ARCHIVE_FILE.parent.mkdir(exist_ok=True)
            tmp = ARCHIVE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(list(self.archive.values())), encoding="utf-8")
            tmp.replace(ARCHIVE_FILE)
        except Exception as e:
            print("Could not save local archive:", e)

    def interval(self, src):
        if src.get("refresh_seconds"):
            return src["refresh_seconds"]
        tier = next((t for t in self.cfg["tiers"] if t["id"] == src["tier"]), {})
        return tier.get("refresh_seconds", 3600)

    def due(self, now):
        return [s for s in self.cfg["sources"]
                if now - self.state.get(s["id"], {}).get("fetched", 0) >= self.interval(s)]

    # -- fetching
    @staticmethod
    def fetch_parse(src, attempts=2):
        """Fetch and parse one source, retrying once after a short pause. Feeds such as
        UN News occasionally answer with a truncated or non-XML response that is fine a
        moment later. Blocked or misconfigured sites fail both attempts and are reported."""
        for n in range(attempts):
            try:
                return PARSERS[src.get("type", "rss")](src, fetch(src["url"], src.get("user_agent")))
            except Exception:
                if n == attempts - 1:
                    raise
                time.sleep(3)

    def fetch_source(self, src, flt):
        t0 = time.time()
        status = {k: src.get(k) for k in ("id", "name", "tier", "kind", "home", "url")}
        prev = self.state.get(src["id"], {})
        try:
            items = self.fetch_parse(src)
            dropped = 0
            if src["tier"] == "local":
                kept = []
                for it in items:
                    ok, why = flt.check(src, it)
                    if ok:
                        it["city"] = flt.is_city(src, it)
                        kept.append(it)
                    else:
                        dropped += 1
                        self.filter_log.append({"source": src["name"], "title": it["title"], "via": it.get("via"), "why": why})
                items = kept
            items.sort(key=lambda i: i["ts"] or 0, reverse=True)
            cap = src.get("max_items", None if src["tier"] == "local" else DEFAULT_MAX_ITEMS)
            if cap:
                items = items[:cap]
            status.update(status="ok", count=len(items), filtered=dropped)
        except Exception as e:  # keep the last good items so a blip doesn't blank a column
            items = prev.get("items", [])
            status.update(status="stale" if items else "error", count=len(items),
                          error=f"{type(e).__name__}: {e}"[:200])
        status["ms"] = int((time.time() - t0) * 1000)
        now = int(time.time())
        status["fetched"] = now
        status["next"] = now + self.interval(src)
        return src, status, items

    def refresh(self, sources):
        if not sources:
            return
        flt = LocalFilter(self.cfg)
        with cf.ThreadPoolExecutor(max_workers=16) as ex:
            results = list(ex.map(lambda s: self.fetch_source(s, flt), sources))
        now = int(time.time())
        with self.lock:
            changed = False
            for src, status, items in results:
                old_ids = {i["id"] for i in self.state.get(src["id"], {}).get("items", [])}
                if {i["id"] for i in items} != old_ids:
                    changed = True
                self.state[src["id"]] = {"items": items, "status": status, "fetched": status["fetched"]}
                if src["tier"] == "local":
                    for it in items:
                        if it["id"] in self.archive:
                            prev = self.archive[it["id"]]
                            it["first_seen"] = prev.get("first_seen", now)
                            it["city"] = it.get("city") or prev.get("city", False)
                        else:
                            it["first_seen"] = now
                            changed = True
                        self.archive[it["id"]] = it
            cutoff = now - ARCHIVE_DAYS * 86400
            self.archive = {k: v for k, v in self.archive.items()
                            if (v.get("ts") or v.get("first_seen", now)) >= cutoff}
            self.filter_log = self.filter_log[-200:]
            if changed:
                self.version += 1
            if any(s["tier"] == "local" for s in sources):
                self._save_archive()

    def tick(self, force=False):
        self.cfg = load_config()
        now = time.time()
        self.refresh(self.cfg["sources"] if force else self.due(now))

    def run_forever(self):
        while True:
            try:
                self.tick()
            except Exception as e:
                print("Scheduler error:", e)
            time.sleep(SCHED_TICK)

    # -- payload
    def payload(self):
        with self.lock:
            src_ids = {s["id"] for s in self.cfg["sources"]}
            statuses = [self.state[s["id"]]["status"] for s in self.cfg["sources"] if s["id"] in self.state]
            items, seen = [], set()
            local = [i for i in self.archive.values() if i["source"] in src_ids]
            others = [i for sid, st in self.state.items() for i in st["items"] if i["tier"] != "local"]
            for it in sorted(local + others, key=lambda i: i["ts"] or 0, reverse=True):
                key = (it["tier"], re.sub(r"\W+", " ", it["title"].lower()).strip())
                if key in seen:
                    continue
                seen.add(key)
                items.append(it)
            tiers = []
            for t in self.cfg["tiers"]:
                sts = [s for s in statuses if s["tier"] == t["id"]]
                tiers.append(dict(t, last=max((s["fetched"] for s in sts), default=None),
                                  next=min((s["next"] for s in sts), default=None)))
            return {
                "generated": int(time.time()),
                "version": self.version,
                "tiers": tiers,
                "sources": statuses,
                "items": items,
                "filtered": self.filter_log[-60:][::-1],
            }

    def get(self, force=False):
        if force:
            self.tick(force=True)
        elif not self.state:
            self.tick()
        return self.payload()

    # -- static build (GitHub Actions + Pages)
    def export_state(self):
        with self.lock:
            return {"saved": int(time.time()), "version": self.version, "state": self.state,
                    "archive": list(self.archive.values()), "filtered": self.filter_log[-200:]}

    def import_state(self, data):
        with self.lock:
            self.version = data.get("version", 0)
            self.state = data.get("state", {})
            self.archive = {i["id"]: i for i in data.get("archive", [])}
            self.filter_log = data.get("filtered", [])


def load_remote_state(url):
    """Fetch the previous run's state.json from the live site. A missing file (first run) is fine."""
    try:
        return json.loads(fetch(url))
    except Exception as e:
        print(f"No previous state at {url} ({type(e).__name__}); starting fresh.")
        return None


def build(out_dir, state_url=None, force=False, run_minutes=10):
    """One refresh cycle for the hosted site: restore the last state, refresh the sources
    that are due (or all of them with force), and write feed.json, state.json and index.html."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if not state_url:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if "/" in repo:
            owner, name = repo.split("/", 1)
            state_url = f"https://{owner.lower()}.github.io/{name}/state.json"
    prev = load_remote_state(state_url) if state_url else None
    if prev:
        AGG.import_state(prev)
    AGG.tick(force=force)
    data = AGG.payload()
    repo = os.environ.get("GITHUB_REPOSITORY")
    data["hosted"] = {
        "repo": repo,
        "run_minutes": run_minutes,
        "actions_url": f"https://github.com/{repo}/actions/workflows/refresh.yml" if repo else None,
    }
    (out / "feed.json").write_text(json.dumps(data), encoding="utf-8")
    (out / "state.json").write_text(json.dumps(AGG.export_state()), encoding="utf-8")
    (out / "index.html").write_bytes(INDEX_FILE.read_bytes())
    (out / ".nojekyll").write_text("", encoding="utf-8")
    ok = sum(s["status"] == "ok" for s in data["sources"])
    local = sum(1 for i in data["items"] if i["tier"] == "local")
    print(f"Built {out}: {len(data['items'])} items ({local} Orange & OC) from {ok}/{len(data['sources'])} sources")
    for s in data["sources"]:
        if s["status"] != "ok":
            print(f"  {s['status']:5s} {s['name']}: {s.get('error', '')}")


AGG = Aggregator()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self.send(200, INDEX_FILE.read_bytes(), "text/html; charset=utf-8")
        elif u.path == "/api/feed":
            force = parse_qs(u.query).get("refresh", ["0"])[0] == "1"
            body = json.dumps(AGG.get(force=force)).encode("utf-8")
            self.send(200, body, "application/json")
        else:
            self.send(404, b"Not found", "text/plain")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--snapshot", metavar="FILE", help="fetch once, write the JSON to FILE, and exit")
    ap.add_argument("--build", metavar="DIR", help="hosted mode: refresh due sources and write a static site to DIR")
    ap.add_argument("--state-url", help="with --build: where the previous state.json lives (defaults to the repo's GitHub Pages URL)")
    ap.add_argument("--force", action="store_true", help="with --build: refresh every source, not just the ones due")
    args = ap.parse_args()

    if args.build:
        build(args.build, args.state_url, args.force)
        return

    if args.snapshot:
        data = AGG.get(force=True)
        Path(args.snapshot).write_text(json.dumps(data), encoding="utf-8")
        ok = sum(s["status"] == "ok" for s in data["sources"])
        print(f"{len(data['items'])} items from {ok}/{len(data['sources'])} sources -> {args.snapshot}")
        for s in data["sources"]:
            if s["status"] != "ok":
                print(f"  {s['status']:5s} {s['name']}: {s.get('error', '')}")
        return

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"Orange Watch running at {url}  (Ctrl+C to stop)")
    threading.Thread(target=AGG.run_forever, daemon=True).start()  # per-tier refresh loop
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
