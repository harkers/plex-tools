#!/usr/bin/env python3
"""Find Plex items that are matched but missing cast/director/genre, optionally refresh them.

A "bare match" is an item whose primary guid is a Plex GUID (plex://movie/... or
plex://show/...) — i.e. the Plex agent successfully matched it — but whose Role,
Director, and Genre arrays are all empty. This happens when the metadata agent
matches an item but the follow-up enrichment pass fails (transient network
error, rate limit, big-ingest backlog).

The fix is a forced refresh: PUT /library/metadata/{ratingKey}/refresh.

Usage:
    PLEX_TOKEN=xxx python3 refresh-bare-matches.py --section 1
    PLEX_TOKEN=xxx python3 refresh-bare-matches.py --section 1 --since 7d
    PLEX_TOKEN=xxx python3 refresh-bare-matches.py --section 1 --since 7d --apply
    PLEX_TOKEN=xxx python3 refresh-bare-matches.py --section 1 --limit 50 --verbose

Dry-run by default. Pass --apply to issue refresh PUTs.
"""

import argparse
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400")
TOKEN = os.environ.get("PLEX_TOKEN", "")
if not TOKEN:
    raise SystemExit("Set PLEX_TOKEN")


def get(path, **q):
    q["X-Plex-Token"] = TOKEN
    url = f"{PLEX_URL}{path}?{urllib.parse.urlencode(q)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return ET.fromstring(r.read())


def put(path, **q):
    q["X-Plex-Token"] = TOKEN
    url = f"{PLEX_URL}{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, method="PUT")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def parse_since(s):
    """Accept '7d', '24h', '30m', or an int (seconds). Return seconds."""
    if not s:
        return None
    m = re.fullmatch(r"(\d+)\s*([dhms]?)", s.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"bad --since value: {s!r}")
    n = int(m.group(1))
    unit = m.group(2) or "s"
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def is_bare(meta):
    """meta is the <Video> or <Directory> element from /library/metadata/{rk}."""
    guid = meta.get("guid", "")
    if not (guid.startswith("plex://movie/") or guid.startswith("plex://show/")):
        return False
    has_role = meta.find("Role") is not None
    has_director = meta.find("Director") is not None
    has_genre = meta.find("Genre") is not None
    return not (has_role or has_director or has_genre)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--section", type=int, required=True, help="Plex library section ID"
    )
    ap.add_argument(
        "--since",
        type=parse_since,
        default=None,
        help="Only check items added within this window (e.g. 7d, 24h)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap items checked (after --since filter)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Issue refresh PUTs (default: dry-run, just list)",
    )
    ap.add_argument(
        "--throttle",
        type=float,
        default=0.1,
        help="Delay between per-item metadata fetches and refresh PUTs",
    )
    ap.add_argument(
        "--verbose", action="store_true", help="Print file path for each bare item"
    )
    args = ap.parse_args()

    listing = get(f"/library/sections/{args.section}/all")
    items = listing.findall("Video") + listing.findall("Directory")
    total = len(items)

    if args.since is not None:
        cutoff = int(time.time()) - args.since
        items = [it for it in items if int(it.get("addedAt", "0")) >= cutoff]
        print(
            f"section {args.section}: {len(items)} of {total} items added within --since window",
            file=sys.stderr,
        )
    else:
        print(f"section {args.section}: scanning all {total} items", file=sys.stderr)

    if args.limit:
        items = items[: args.limit]

    bare = []
    for i, it in enumerate(items, 1):
        rk = it.get("ratingKey")
        if not rk:
            continue
        try:
            meta_root = get(f"/library/metadata/{rk}")
        except Exception as e:
            print(f"  rk={rk} fetch failed: {e}", file=sys.stderr)
            continue
        meta = meta_root.find("Video") or meta_root.find("Directory")
        if meta is None:
            continue
        if is_bare(meta):
            bare.append(meta)
        if i % 100 == 0:
            print(
                f"  scanned {i}/{len(items)} — {len(bare)} bare so far", file=sys.stderr
            )
        if args.throttle:
            time.sleep(args.throttle)

    print(f"\nBare matches: {len(bare)}")
    for meta in bare:
        rk = meta.get("ratingKey")
        title = meta.get("title", "?")
        year = meta.get("year", "")
        guid = meta.get("guid", "")
        line = f"  rk={rk:<8} {title} ({year})  {guid}"
        print(line)
        if args.verbose:
            part = meta.find(".//Part")
            f = part.get("file") if part is not None else "?"
            print(f"      {f}")

    if not args.apply:
        if bare:
            print(f"\n(dry-run — pass --apply to refresh {len(bare)} item(s))")
        return

    print(f"\nRefreshing {len(bare)} item(s)...")
    ok = fail = 0
    for meta in bare:
        rk = meta.get("ratingKey")
        try:
            status = put(f"/library/metadata/{rk}/refresh")
            if 200 <= status < 300:
                ok += 1
            else:
                fail += 1
                print(f"  rk={rk} HTTP {status}", file=sys.stderr)
        except Exception as e:
            fail += 1
            print(f"  rk={rk} refresh failed: {e}", file=sys.stderr)
        if args.throttle:
            time.sleep(args.throttle)

    print(f"refresh: ok={ok} fail={fail}")


if __name__ == "__main__":
    main()
