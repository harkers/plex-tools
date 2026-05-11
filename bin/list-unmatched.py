#!/usr/bin/env python3
"""List unmatched items in a Plex movie library section.

Usage:
    PLEX_TOKEN=xxx python3 list-unmatched.py --section 1
    PLEX_TOKEN=xxx python3 list-unmatched.py --section 1 --limit 30 --verbose
    PLEX_TOKEN=xxx python3 list-unmatched.py --counts  # totals across all movie sections
"""
import argparse
import os
import urllib.request
import urllib.parse
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", type=int, help="Movie section id (use --counts to list all)")
    ap.add_argument("--counts", action="store_true", help="Show unmatched counts across all movie sections")
    ap.add_argument("--limit", type=int, default=20, help="Per-section listing cap")
    ap.add_argument("--verbose", action="store_true", help="Show full file path per item")
    args = ap.parse_args()

    if args.counts or not args.section:
        all_sections = get("/library/sections/all")
        print(f"{'ID':>4} {'TYPE':<6} {'TITLE':<25} UNMATCHED")
        for d in all_sections.findall("Directory"):
            if d.get("type") != "movie":
                continue
            sid = d.get("key")
            t = d.get("title")
            root = get(f"/library/sections/{sid}/all", unmatched=1)
            count = int(root.get("totalSize", root.get("size", "0")))
            print(f"{sid:>4} {'movie':<6} {t:<25} {count}")
        return

    root = get(f"/library/sections/{args.section}/all", unmatched=1)
    total = int(root.get("totalSize", root.get("size", "0")))
    print(f"section {args.section}: {total} unmatched\n")
    for v in root.findall("Video")[: args.limit]:
        rk = v.get("ratingKey")
        title = v.get("title", "?")
        year = v.get("year", "")
        line = f"  rk={rk:<8} {title} ({year})"
        print(line)
        if args.verbose:
            parts = v.findall(".//Part")
            f = parts[0].get("file") if parts else "?"
            print(f"      {f}")
    if total > args.limit:
        print(f"\n  ... +{total - args.limit} more (use --limit to see more)")


if __name__ == "__main__":
    main()
