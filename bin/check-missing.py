#!/usr/bin/env python3
"""Report missing posters/backdrops/summaries for a Plex library section.

Usage:
    PLEX_TOKEN=xxx python3 check-missing.py --section 5
"""
import argparse, urllib.request, os, xml.etree.ElementTree as ET

PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400")
TOKEN = os.environ.get("PLEX_TOKEN", "")
if not TOKEN: raise SystemExit("Set PLEX_TOKEN")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", type=int, required=True)
    ap.add_argument("--show-missing", action="store_true", help="List first 40 missing items")
    args = ap.parse_args()

    url = f"{PLEX_URL}/library/sections/{args.section}/all?X-Plex-Token={TOKEN}"
    body = urllib.request.urlopen(url, timeout=240).read()
    r = ET.fromstring(body)
    items = r.findall("Directory") + r.findall("Video")

    no_p = [s for s in items if not (s.get("thumb","") or "").strip()]
    no_a = [s for s in items if not (s.get("art","") or "").strip()]
    no_s = [s for s in items if len((s.get("summary","") or "").strip()) < 30]

    print(f"Total: {len(items)}")
    print(f"Missing poster:  {len(no_p)}")
    print(f"Missing art:     {len(no_a)}")
    print(f"Missing summary: {len(no_s)}")

    if args.show_missing:
        print("\n--- First 40 with missing poster ---")
        for s in no_p[:40]:
            print(f"  rk={s.get('ratingKey')} {s.get('title')} ({s.get('year','?')})")


if __name__ == "__main__":
    main()
