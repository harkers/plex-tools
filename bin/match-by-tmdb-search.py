#!/usr/bin/env python3
"""Force-match unmatched Plex movies by searching TMDb for title+year.

Confidence rule: normalised-title exact match AND year within +/-1.

Usage:
    PLEX_TOKEN=xxx TMDB_KEY=yyy python3 match-by-tmdb-search.py --section 1
    PLEX_TOKEN=xxx TMDB_KEY=yyy python3 match-by-tmdb-search.py --section 1 --apply
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
TMDB_KEY = os.environ.get("TMDB_KEY", "")
if not PLEX_TOKEN:
    raise SystemExit("Set PLEX_TOKEN")
if not TMDB_KEY:
    raise SystemExit("Set TMDB_KEY")


def plex(method, path, **q):
    q["X-Plex-Token"] = PLEX_TOKEN
    url = f"{PLEX_URL}{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            text = r.read()
            return r.getcode(), (ET.fromstring(text) if text else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def tmdb_search(title, year):
    q = {"api_key": TMDB_KEY, "query": title}
    if year:
        q["year"] = str(year)
    url = "https://api.themoviedb.org/3/search/movie?" + urllib.parse.urlencode(q)
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read())
    except Exception as e:
        return [{"error": str(e)}]
    out = []
    for m in data.get("results", []):
        rd = m.get("release_date") or ""
        ry = int(rd[:4]) if rd[:4].isdigit() else None
        out.append({"id": m["id"], "title": m.get("title"), "year": ry, "pop": m.get("popularity", 0)})
    return out


def norm(s):
    s = (s or "").lower()
    s = re.sub(r"^(the|a|an)\s+", "", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def confident(plex_title, plex_year, hit):
    if not hit.get("title") or norm(hit["title"]) != norm(plex_title):
        return False
    if plex_year and hit.get("year"):
        return abs(int(plex_year) - hit["year"]) <= 1
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", type=int, required=True)
    ap.add_argument("--apply", action="store_true", help="Commit changes (default is dry-run)")
    args = ap.parse_args()

    code, root = plex("GET", f"/library/sections/{args.section}/all", unmatched=1)
    if not isinstance(root, ET.Element):
        sys.exit(f"could not fetch unmatched: {code} {root}")
    videos = root.findall("Video")
    print(f"unmatched in section {args.section}: {len(videos)}")
    print(f"mode: {'APPLY' if args.apply else 'DRY-RUN (use --apply)'}\n")

    matched = no_hit = uncertain = api_fail = 0
    for v in videos:
        rk = v.get("ratingKey")
        title = v.get("title", "?")
        year = v.get("year", "")
        hits = tmdb_search(title, year)
        if hits and "error" in hits[0]:
            print(f"  X rk={rk} {title!r}  tmdb-error: {hits[0]['error']}")
            api_fail += 1
            continue
        good = next((h for h in hits if confident(title, year, h)), None)
        if not good:
            top = hits[0] if hits else None
            if top:
                print(f"  ? rk={rk} {title!r} ({year})  uncertain: top tmdb = {top['title']!r} ({top['year']})")
                uncertain += 1
            else:
                print(f"  - rk={rk} {title!r} ({year})  no tmdb hits")
                no_hit += 1
            continue
        if not args.apply:
            print(f"  + rk={rk} {title!r}  WOULD MATCH tmdb://{good['id']} ({good['title']!r}, {good['year']})")
            matched += 1
            continue
        c, body = plex(
            "PUT",
            f"/library/metadata/{rk}/match",
            guid=f"tmdb://{good['id']}",
            name=good["title"],
            year=str(good["year"]),
        )
        if c in (200, 204):
            print(f"  + rk={rk} {title!r} -> tmdb://{good['id']} ({good['title']!r}, {good['year']})")
            matched += 1
        elif c == 500:
            # Plex sometimes 500s on tmdb internal lookup; try imdb fallback if discoverable
            print(f"  X rk={rk} {title!r}  tmdb 500 — try IMDb GUID manually")
            api_fail += 1
        else:
            print(f"  X rk={rk} {title!r}  HTTP {c}  {str(body)[:150]}")
            api_fail += 1
        time.sleep(0.2)

    print(f"\nmatched: {matched}   no-tmdb-hit: {no_hit}   uncertain: {uncertain}   api-failures: {api_fail}")


if __name__ == "__main__":
    main()
