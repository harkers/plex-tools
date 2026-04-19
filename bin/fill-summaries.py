#!/usr/bin/env python3
"""fill-summaries.py — fill missing Plex TV/movie summaries via plexapi + TMDb.

Part of the IPTV artwork backfill workflow. Run AFTER Kometa's mass_poster_update
+ mass_background_update operations have populated artwork. Kometa has no
mass_summary_update for TV shows; this script fills the gap.

Strategy:
  - Iterate every item in the target Plex section
  - Skip items that already have a summary of min-length or more
  - For each item, extract external IDs from plexapi's .guids
  - Prefer tmdb:// → direct /tv/{id} or /movie/{id}
  - Fall back to tvdb:// → /find?external_source=tvdb_id
  - Fall back to imdb://ttXXXX → /find?external_source=imdb_id
  - Skip items with no external ID (local:// entries)
  - On success, show.editSummary(overview, locked=True) so Plex agent won't overwrite

Uses python-plexapi (proper API client) + requests.Session (connection pooling)
+ tenacity (exponential backoff on 429s with Retry-After respect).

Usage:
    PLEX_TOKEN=xxx TMDB_READ_TOKEN=xxx python3 fill-summaries.py --section 5
    PLEX_TOKEN=xxx TMDB_READ_TOKEN=xxx python3 fill-summaries.py --section 1 --type movie
"""
import argparse, os, sys, time, logging
from plexapi.server import PlexServer
import requests
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
TMDB_TOKEN = os.environ.get("TMDB_READ_TOKEN", "")

if not PLEX_TOKEN or not TMDB_TOKEN:
    sys.exit("Set PLEX_TOKEN and TMDB_READ_TOKEN")


# Session with Bearer auth
tmdb_sess = requests.Session()
tmdb_sess.headers.update({
    "Authorization": f"Bearer {TMDB_TOKEN}",
    "Accept": "application/json",
})


class TmdbRateLimit(Exception): pass


@retry(
    wait=wait_exponential(multiplier=2, min=2, max=120),
    retry=retry_if_exception_type((requests.HTTPError, TmdbRateLimit,
                                    requests.exceptions.ConnectionError)),
    stop=stop_after_attempt(5),
    reraise=True,
)
def tmdb(path, **params):
    r = tmdb_sess.get(f"https://api.themoviedb.org/3{path}", params=params, timeout=15)
    if r.status_code == 429:
        wait = int(r.headers.get("Retry-After", 10))
        logging.warning(f"TMDb 429 — Retry-After {wait}s")
        time.sleep(wait)
        raise TmdbRateLimit()
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def extract_ids(item):
    """plexapi items expose .guids list — normalize to dict {tmdb: x, tvdb: y, imdb: z}."""
    out = {}
    for g in item.guids or []:
        gid = getattr(g, "id", "")
        if gid.startswith("tmdb://"): out["tmdb"] = gid.removeprefix("tmdb://")
        elif gid.startswith("tvdb://"): out["tvdb"] = gid.removeprefix("tvdb://")
        elif gid.startswith("imdb://"): out["imdb"] = gid.removeprefix("imdb://")
    # Also check primary guid (for legacy agents)
    main = getattr(item, "guid", "") or ""
    if "tmdb://" in main and "tmdb" not in out:
        out["tmdb"] = main.split("tmdb://")[-1].split("?")[0]
    elif "tvdb://" in main and "tvdb" not in out:
        out["tvdb"] = main.split("tvdb://")[-1].split("?")[0]
    elif "thetvdb://" in main and "tvdb" not in out:
        out["tvdb"] = main.split("thetvdb://")[-1].split("?")[0]
    elif "themoviedb://" in main and "tmdb" not in out:
        out["tmdb"] = main.split("themoviedb://")[-1].split("?")[0]
    return out


def resolve_overview(ids, media_type):
    """Resolve external IDs → TMDb detail → overview string. Returns (overview, method) or (None, None)."""
    endpoint = "/tv" if media_type == "show" else "/movie"

    # 1. Direct tmdb ID
    if "tmdb" in ids and ids["tmdb"].isdigit():
        d = tmdb(f"{endpoint}/{ids['tmdb']}")
        if d and d.get("overview"):
            return d["overview"].strip(), "direct-tmdb"

    # 2. TVDB find
    if "tvdb" in ids and ids["tvdb"].isdigit():
        d = tmdb(f"/find/{ids['tvdb']}", external_source="tvdb_id")
        if d:
            key = "tv_results" if media_type == "show" else "movie_results"
            results = d.get(key, [])
            if results and results[0].get("overview"):
                return results[0]["overview"].strip(), "find-tvdb"
            elif results:
                # Have TMDb ID now, fetch full
                d2 = tmdb(f"{endpoint}/{results[0]['id']}")
                if d2 and d2.get("overview"):
                    return d2["overview"].strip(), "find-tvdb-detail"

    # 3. IMDb find
    if "imdb" in ids and ids["imdb"].startswith("tt"):
        d = tmdb(f"/find/{ids['imdb']}", external_source="imdb_id")
        if d:
            key = "tv_results" if media_type == "show" else "movie_results"
            results = d.get(key, [])
            if results and results[0].get("overview"):
                return results[0]["overview"].strip(), "find-imdb"

    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", type=int, required=True, help="Plex section ID")
    ap.add_argument("--type", choices=["show", "movie"], default="show")
    ap.add_argument("--min-length", type=int, default=30,
                    help="Chars to consider summary present (below → fill)")
    ap.add_argument("--throttle", type=float, default=0.25, help="Delay between items")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log", default="/tmp/fill-summaries.log")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(args.log, mode="w"), logging.StreamHandler()],
    )
    log = logging.getLogger()

    log.info(f"Connecting to {PLEX_URL}")
    plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=120)

    # Find section
    section = plex.library.sectionByID(args.section)
    log.info(f"Section: {section.title} ({section.type})")

    # Iterate all items
    items = section.all()
    log.info(f"Total items: {len(items)}")

    targets = [it for it in items
               if not (it.summary and len(it.summary.strip()) >= args.min_length)]
    log.info(f"Missing/thin summary: {len(targets)}")

    if args.dry_run:
        for t in targets[:20]:
            ids = extract_ids(t)
            log.info(f"  rk={t.ratingKey} {t.title!r} ids={ids}")
        return

    stats = {"skipped_no_id": 0, "no_overview": 0,
             "direct-tmdb": 0, "find-tvdb": 0, "find-tvdb-detail": 0, "find-imdb": 0,
             "put_ok": 0, "put_fail": 0}
    t0 = time.time()

    for i, item in enumerate(targets, 1):
        if i % 25 == 0:
            elapsed = (time.time() - t0) / 60
            log.info(f"  {i}/{len(targets)} elapsed={elapsed:.1f}m — {stats}")

        ids = extract_ids(item)
        if not ids:
            stats["skipped_no_id"] += 1
            continue

        try:
            overview, method = resolve_overview(ids, args.type)
        except Exception as e:
            log.warning(f"  rk={item.ratingKey} TMDb failed: {str(e)[:100]}")
            stats["no_overview"] += 1
            continue

        if not overview or len(overview) < args.min_length:
            stats["no_overview"] += 1
            continue

        stats[method] = stats.get(method, 0) + 1

        try:
            # plexapi's editSummary handles the PUT + lock automatically
            item.editSummary(overview[:2000], locked=True)
            stats["put_ok"] += 1
        except Exception as e:
            log.warning(f"  rk={item.ratingKey} edit failed: {str(e)[:100]}")
            stats["put_fail"] += 1

        time.sleep(args.throttle)

    log.info(f"\n=== DONE — elapsed {(time.time() - t0) / 60:.1f}m ===")
    for k, v in stats.items():
        log.info(f"  {k}: {v}")


if __name__ == "__main__":
    main()
