#!/usr/bin/env python3
"""Force-match unmatched Plex movies using IMDb tt-ID embedded in the filename.

Looks for {imdb-ttNNNNN} (canonical) or (imbd-ttNNNNN) (typo variant) in each
file path and PUTs match with guid=imdb://ttNNNNN.

Usage:
    PLEX_TOKEN=xxx python3 match-by-filename.py --section 1            # dry-run
    PLEX_TOKEN=xxx python3 match-by-filename.py --section 1 --apply    # commit
"""
import argparse
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400")
TOKEN = os.environ.get("PLEX_TOKEN", "")
if not TOKEN:
    raise SystemExit("Set PLEX_TOKEN")

# Accepts {imdb-tt...} (canonical) and (imbd-tt...) (typo variant in legacy filenames)
IMDB_RE = re.compile(r"[{(](?:imdb|imbd)-(tt\d+)[})]")


def call(method, path, **q):
    q["X-Plex-Token"] = TOKEN
    url = f"{PLEX_URL}{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            text = r.read()
            return r.getcode(), (ET.fromstring(text) if text else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except (TimeoutError, OSError) as e:
        return -1, f"network: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", type=int, required=True)
    ap.add_argument("--apply", action="store_true", help="Commit changes (default is dry-run)")
    args = ap.parse_args()

    code, root = call("GET", f"/library/sections/{args.section}/all", unmatched=1)
    if not isinstance(root, ET.Element):
        sys.exit(f"could not fetch unmatched: {code} {root}")
    videos = root.findall("Video")
    print(f"unmatched in section {args.section}: {len(videos)}")
    print(f"mode: {'APPLY' if args.apply else 'DRY-RUN (use --apply)'}\n")

    matched = no_imdb = api_fail = 0
    for v in videos:
        rk = v.get("ratingKey")
        title = v.get("title", "?")
        year = v.get("year", "")
        parts = v.findall(".//Part")
        f = parts[0].get("file") if parts else ""
        m = IMDB_RE.search(f or "")
        imdb = m.group(1) if m else None

        if not imdb:
            print(f"  ! rk={rk} {title!r}  no-imdb-in-path")
            no_imdb += 1
            continue

        if not args.apply:
            print(f"  + rk={rk} {title!r}  WOULD MATCH imdb://{imdb}")
            matched += 1
            continue

        c, body = call(
            "PUT",
            f"/library/metadata/{rk}/match",
            guid=f"imdb://{imdb}",
            name=title.lstrip("'\""),
            year=str(year),
        )
        if c in (200, 204):
            print(f"  + rk={rk} {title!r}  matched -> imdb://{imdb}")
            matched += 1
        else:
            print(f"  X rk={rk} {title!r}  HTTP {c}  {str(body)[:150]}")
            api_fail += 1
        time.sleep(0.15)

    print(f"\nmatched: {matched}   no-imdb-in-path: {no_imdb}   api-failures: {api_fail}")


if __name__ == "__main__":
    main()
