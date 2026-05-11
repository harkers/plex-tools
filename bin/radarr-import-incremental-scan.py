#!/usr/bin/env python3
"""Path-scoped Plex scan triggered by Radarr's recent imports.

Polls Radarr's history for `downloadFolderImported` events since the last run,
collects the unique parent directories, and tells Plex to refresh ONLY those
paths instead of walking all 8 Movies-section locations.

Way faster for incremental updates than a section-wide refresh, especially
while a deep-analysis backlog is keeping Plex CPU-pegged.

Designed to run as a cron job every 5-10 minutes during heavy download windows.

Usage:
    PLEX_TOKEN=xxx RADARR_KEY=yyy python3 radarr-import-incremental-scan.py
    # state in /var/tmp/radarr-plex-incremental.state (override with STATE_FILE env)

Env vars:
    RADARR_URL    default http://192.168.10.29:7878
    RADARR_KEY    required
    PLEX_URL      default http://127.0.0.1:32400 (set to titan IP if running off-host)
    PLEX_TOKEN    required
    PLEX_MOVIE_SECTION  default 1
    STATE_FILE    default /var/tmp/radarr-plex-incremental.state
    LOOKBACK_HOURS  default 1 (only used on first run when state file is missing)
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

RADARR_URL = os.environ.get("RADARR_URL", "http://192.168.10.29:7878")
RADARR_KEY = os.environ.get("RADARR_KEY", "")
PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
PLEX_MOVIE_SECTION = int(os.environ.get("PLEX_MOVIE_SECTION", "1"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "/var/tmp/radarr-plex-incremental.state"))
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "1"))

# Radarr container sees /data/... ; Plex container sees /media/...
# Add more pairs here if you have other mount layouts.
PATH_MAP = [
    ("/data/nas3-storage/", "/media/nas3-storage/"),
    ("/data/athena-storage/", "/media/athena-storage/"),
    ("/data/athena/", "/media/athena/"),
    ("/mnt/nas3-storage/", "/media/nas3-storage/"),
    ("/mnt/athena-storage/", "/media/athena-storage/"),
    ("/mnt/athena/", "/media/athena/"),
]

# Radarr history event types
EVENT_DOWNLOAD_FOLDER_IMPORTED = 3


def radarr(path):
    req = urllib.request.Request(
        f"{RADARR_URL}{path}",
        headers={"X-Api-Key": RADARR_KEY},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def plex_refresh_path(section_id, path):
    """Trigger a Plex section refresh scoped to one path. Returns HTTP code."""
    q = {"path": path, "X-Plex-Token": PLEX_TOKEN}
    url = f"{PLEX_URL}/library/sections/{section_id}/refresh?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.getcode()
    except Exception as e:
        return f"ERR {e}"


def translate(radarr_path):
    """Map a Radarr-side path to the Plex container's view."""
    for src, dst in PATH_MAP:
        if radarr_path.startswith(src):
            return dst + radarr_path[len(src):]
    return radarr_path


def load_last_processed():
    if STATE_FILE.exists():
        return STATE_FILE.read_text().strip()
    # First run: scan everything within the lookback window
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def save_last_processed(iso_ts):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(iso_ts)


def main():
    if not RADARR_KEY:
        sys.exit("Set RADARR_KEY")
    if not PLEX_TOKEN:
        sys.exit("Set PLEX_TOKEN")

    last_processed = load_last_processed()
    print(f"[*] last processed: {last_processed}")

    # Fetch the most recent imports
    data = radarr(
        f"/api/v3/history?eventType={EVENT_DOWNLOAD_FOLDER_IMPORTED}"
        "&pageSize=100&sortKey=date&sortDirection=descending"
    )
    records = data.get("records", [])
    fresh = [r for r in records if (r.get("date") or "") > last_processed]
    fresh.sort(key=lambda r: r.get("date", ""))

    print(f"[*] new import events: {len(fresh)}")
    if not fresh:
        return

    # Group by parent directory to dedupe scans
    paths_seen = {}
    for ev in fresh:
        movie_id = ev.get("movieId")
        if not movie_id:
            continue
        try:
            movie = radarr(f"/api/v3/movie/{movie_id}")
        except Exception as e:
            print(f"  ! could not fetch movie {movie_id}: {e}")
            continue
        movie_dir = movie.get("path")
        if not movie_dir:
            continue
        plex_dir = translate(movie_dir)
        # Use the parent (root folder) as the scan target — covers any sibling
        # files in the same folder if Radarr does an Atomic Move during import.
        paths_seen.setdefault(plex_dir, []).append(movie.get("title", "?"))

    # Trigger scans
    print(f"[*] unique paths to scan: {len(paths_seen)}")
    for path, titles in sorted(paths_seen.items()):
        code = plex_refresh_path(PLEX_MOVIE_SECTION, path)
        sample = titles[0] + (" +" + str(len(titles) - 1) + " more" if len(titles) > 1 else "")
        print(f"  [{code}] {path}   ({sample})")

    # Persist the latest event date as the next baseline
    latest = max(r.get("date", "") for r in fresh)
    save_last_processed(latest)
    print(f"[*] state advanced to: {latest}")


if __name__ == "__main__":
    main()
