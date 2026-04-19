#!/usr/bin/env python3
"""Split a wrongly-merged Plex show back into its component items.

When Plex merges multiple folders into one show because the filenames share
a prefix (e.g. 'Blindspot' + 'Blindspotting'), this undoes the merge.

Usage:
    PLEX_TOKEN=xxx python3 split-merged.py <rating_key>

After split, each original folder becomes its own item. Use fix-shows.py
to re-match any items to the correct TMDb entry.
"""
import sys, urllib.request, os, xml.etree.ElementTree as ET

PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400")
TOKEN = os.environ.get("PLEX_TOKEN", "")
if not TOKEN: raise SystemExit("Set PLEX_TOKEN")
if len(sys.argv) < 2: raise SystemExit("Usage: split-merged.py <rating_key>")

rk = sys.argv[1]

# Show current state
pre = urllib.request.urlopen(f"{PLEX_URL}/library/metadata/{rk}?X-Plex-Token={TOKEN}", timeout=30).read()
r = ET.fromstring(pre)
d = r.find("Directory") or r.find("Video")
if d is None: raise SystemExit(f"rk={rk} not found")

print(f"Before split:")
print(f"  title={d.get('title')} year={d.get('year')} eps={d.get('leafCount','?')}")
for loc in d.findall("Location"):
    print(f"  FILE: {loc.get('path')}")

# Split
req = urllib.request.Request(f"{PLEX_URL}/library/metadata/{rk}/split?X-Plex-Token={TOKEN}", method="PUT")
resp = urllib.request.urlopen(req, timeout=30)
print(f"\nSplit request: HTTP {resp.getcode()}")
