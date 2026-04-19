#!/usr/bin/env python3
"""Re-match specific Plex items to correct TMDb IDs + upload fresh artwork.

Use when a show was matched to the wrong TMDb entry (e.g. TMDb ID collision,
wrong match after split). Supply `rk:tmdb_id` pairs.

Usage:
    PLEX_TOKEN=xxx TMDB_APIKEY=xxx python3 fix-shows.py 286888:2327 326171:93740

Each pair: <rating_key>:<correct_tmdb_id>. Script will:
    1. PUT /match to bind the rk to the TMDb guid
    2. Upload TMDb poster + backdrop
    3. PUT correct title + year + summary + locked
"""
import sys, os, json, urllib.request, urllib.parse, xml.etree.ElementTree as ET

PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400")
TOKEN = os.environ.get("PLEX_TOKEN", "")
TMDB = os.environ.get("TMDB_APIKEY", "")
if not TOKEN or not TMDB: raise SystemExit("Set PLEX_TOKEN + TMDB_APIKEY")
if len(sys.argv) < 2: raise SystemExit("Usage: fix-shows.py <rk>:<tmdb_id> [...]")

def plex(path, method="GET"):
    url = f"{PLEX_URL}{path}{'&' if '?' in path else '?'}X-Plex-Token={TOKEN}"
    req = urllib.request.Request(url, method=method)
    resp = urllib.request.urlopen(req, timeout=30)
    return resp.getcode(), resp.read()

def tmdb_tv(tmdb_id):
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={TMDB}"
    return json.loads(urllib.request.urlopen(url, timeout=15).read())

# Try to detect section from first rk
section = None
for pair in sys.argv[1:]:
    rk, tid = pair.split(":")
    # Try TV, fall back to movie
    details = None
    media_type = "tv"
    try:
        details = tmdb_tv(tid)
    except Exception:
        media_type = "movie"
        details = json.loads(urllib.request.urlopen(f"https://api.themoviedb.org/3/movie/{tid}?api_key={TMDB}", timeout=15).read())

    title = details.get("name") or details.get("title","")
    year = (details.get("first_air_date") or details.get("release_date") or "")[:4]
    overview = (details.get("overview","") or "").strip()

    print(f"\n=== rk={rk} → TMDb {media_type} {tid}: {title} ({year}) ===")

    # /match endpoint
    guid = urllib.parse.quote(f"tmdb://{tid}")
    q = f"guid={guid}&name={urllib.parse.quote(title)}"
    if year: q += f"&year={year}"
    code, _ = plex(f"/library/metadata/{rk}/match?{q}", method="PUT")
    print(f"  match: HTTP {code}")

    # upload poster
    if details.get("poster_path"):
        pu = f"https://image.tmdb.org/t/p/original{details['poster_path']}"
        code, _ = plex(f"/library/metadata/{rk}/posters?url={urllib.parse.quote(pu)}", method="POST")
        print(f"  poster: HTTP {code}")

    # upload backdrop
    if details.get("backdrop_path"):
        bu = f"https://image.tmdb.org/t/p/original{details['backdrop_path']}"
        code, _ = plex(f"/library/metadata/{rk}/arts?url={urllib.parse.quote(bu)}", method="POST")
        print(f"  art: HTTP {code}")

    # Find section via current state if we don't know it
    if section is None:
        _, body = plex(f"/library/metadata/{rk}")
        r = ET.fromstring(body)
        d = r.find("Directory") or r.find("Video")
        if d is not None:
            section = d.get("librarySectionID")

    if section and overview:
        ptype = "2" if media_type == "tv" else "1"
        params = {
            "type": ptype, "id": str(rk),
            "title.value": title, "title.locked": "1",
            "summary.value": overview[:2000], "summary.locked": "1",
        }
        if year:
            params["year.value"] = year; params["year.locked"] = "1"
        code, _ = plex(f"/library/sections/{section}/all?{urllib.parse.urlencode(params)}", method="PUT")
        print(f"  metadata lock: HTTP {code}")

print("\nDone. Refresh Plex in 30s to see changes.")
