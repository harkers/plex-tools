#!/usr/bin/env python3
"""Show scan state per Plex section + tail of the scanner log.

Useful during heavy ingest to confirm a scan is making progress.

Usage:
    PLEX_TOKEN=xxx python3 scan-status.py
    PLEX_TOKEN=xxx python3 scan-status.py --type movie
"""
import argparse
import os
import re
import subprocess
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400")
TOKEN = os.environ.get("PLEX_TOKEN", "")
PLEX_CONTAINER = os.environ.get("PLEX_CONTAINER", "plex")
if not TOKEN:
    raise SystemExit("Set PLEX_TOKEN")


def get(path, **q):
    q["X-Plex-Token"] = TOKEN
    url = f"{PLEX_URL}{path}?{urllib.parse.urlencode(q)}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return ET.fromstring(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", choices=("movie", "show", "artist", "all"), default="all")
    ap.add_argument("--log-tail", type=int, default=200, help="Scanner log lines to inspect for active paths")
    args = ap.parse_args()

    print("SECTIONS:")
    root = get("/library/sections/all")
    for d in root.findall("Directory"):
        if args.type != "all" and d.get("type") != args.type:
            continue
        key = d.get("key")
        title = d.get("title")
        refreshing = d.get("refreshing", "0")
        flag = "SCANNING" if refreshing == "1" else "idle"
        scanned = d.get("scannedAt")
        print(f"  section {key:>3} {d.get('type','?'):<6} {title:<25} {flag}  scannedAt={scanned}")

    print("\nACTIVE SCANNER SUBPROCESSES IN CONTAINER:")
    try:
        out = subprocess.run(
            ["docker", "exec", PLEX_CONTAINER, "ps", "-ef"],
            capture_output=True, text=True, timeout=15,
        )
        for line in out.stdout.splitlines():
            if "Plex Media Scanner" in line and "ps -ef" not in line:
                print(f"  {line.strip()}")
    except Exception as e:
        print(f"  (couldn't reach container: {e})")

    print("\nUNIQUE TOP-LEVEL PATHS IN RECENT SCANNER LOG:")
    try:
        out = subprocess.run(
            ["docker", "exec", PLEX_CONTAINER, "tail", f"-{args.log_tail}",
             "/config/Library/Application Support/Plex Media Server/Logs/Plex Media Scanner.log"],
            capture_output=True, text=True, timeout=15,
        )
        paths = set()
        for line in out.stdout.splitlines():
            for m in re.findall(r"/media/[^\"'\s\]]+", line):
                parts = m.split("/")[:5]
                paths.add("/".join(parts))
        for p in sorted(paths):
            print(f"  {p}")
    except Exception as e:
        print(f"  (couldn't read scanner log: {e})")


if __name__ == "__main__":
    main()
