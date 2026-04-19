#!/usr/bin/env python3
"""backfill-artwork.py — fully external bulk artwork + summary backfill for Plex.

Does NOT use Plex's agent refresh. Does its own TMDb lookups, uploads posters
and backdrops to Plex via /posters + /arts POST, and pushes summaries via
/library/sections/N/all PUT with summary.value + summary.locked=1.

Smart lookup strategy:
  1. If item already has tmdb://ID guid → direct TMDb /tv/{id} or /movie/{id}
  2. If item has tvdb://ID or com.plexapp.agents.thetvdb://ID guid
       → TMDb /find/{tvdb_id}?external_source=tvdb_id → get TMDb ID → detail
  3. If item has imdb://ttID guid → TMDb /find with external_source=imdb_id
  4. Fallback: TMDb /search with title + year + fuzzy matching

This avoids rate-limit issues because:
  - Items with external IDs = 1-2 calls (find + detail) with no search, no ambiguity
  - Only ambiguous cases need search
  - Batched with cooldowns between batches
  - Exponential backoff on 429

Usage:
    PLEX_TOKEN=xxx TMDB_APIKEY=xxx python3 backfill-artwork.py --section 5
    PLEX_TOKEN=xxx TMDB_APIKEY=xxx python3 backfill-artwork.py --section 5 --type movie
    PLEX_TOKEN=xxx TMDB_APIKEY=xxx python3 backfill-artwork.py --section 5 --dry-run
"""
import argparse, urllib.request, urllib.parse, urllib.error, json, os, sys, time, difflib, re
import xml.etree.ElementTree as ET

PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400")
TOKEN = os.environ.get("PLEX_TOKEN", "")
TMDB = os.environ.get("TMDB_APIKEY", "")
if not TOKEN: sys.exit("Set PLEX_TOKEN")
if not TMDB: sys.exit("Set TMDB_APIKEY")

GUID_RE = re.compile(r"(tmdb|tvdb|imdb|com\.plexapp\.agents\.thetvdb|com\.plexapp\.agents\.themoviedb|com\.plexapp\.agents\.imdb)://([a-z0-9]+)")


def plex_req(path, method="GET", timeout=60):
    url = f"{PLEX_URL}{path}{'&' if '?' in path else '?'}X-Plex-Token={TOKEN}"
    req = urllib.request.Request(url, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode()


def tmdb(path, **p):
    p["api_key"] = TMDB
    url = f"https://api.themoviedb.org/3{path}?{urllib.parse.urlencode(p)}"
    for attempt in range(4):
        try:
            return json.loads(urllib.request.urlopen(url, timeout=15).read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", 10))
                print(f"    429 — sleeping {wait + 2}s", flush=True)
                time.sleep(wait + 2); continue
            if e.code == 404:
                return None
            return None
        except Exception:
            time.sleep(2 ** attempt)
    return None


def extract_ids(item):
    """Extract (source, id) pairs from Plex item's GUID and Guid children.
    Normalizes agent-prefixed GUIDs to simple tmdb/tvdb/imdb."""
    ids = []

    def normalize(src):
        if "tmdb" in src or "themoviedb" in src: return "tmdb"
        if "tvdb" in src or "thetvdb" in src: return "tvdb"
        if "imdb" in src: return "imdb"
        return src

    main = item.get("guid", "") or ""
    m = GUID_RE.search(main)
    if m:
        src = normalize(m.group(1))
        ids.append((src, m.group(2)))

    for g in item.findall("Guid"):
        gid = g.get("id", "") or ""
        m = GUID_RE.search(gid)
        if m:
            src = normalize(m.group(1))
            pair = (src, m.group(2))
            if pair not in ids:
                ids.append(pair)
    return ids


def resolve_tmdb_id(ids, title, year, media_type):
    """Given Plex IDs, resolve to a TMDb ID. Returns (tmdb_id, method) or (None, None)."""
    # 1. If we already have tmdb ID → use it
    for src, val in ids:
        if src == "tmdb" and val.isdigit():
            return int(val), "direct-tmdb"

    # 2. If we have tvdb → TMDb find
    for src, val in ids:
        if src == "tvdb" and val.isdigit():
            res = tmdb(f"/find/{val}", external_source="tvdb_id")
            if res:
                key = "tv_results" if media_type == "show" else "movie_results"
                items = res.get(key, [])
                if items:
                    return items[0]["id"], f"find-tvdb-{val}"

    # 3. If we have imdb → TMDb find
    for src, val in ids:
        if src == "imdb" and val.startswith("tt"):
            res = tmdb(f"/find/{val}", external_source="imdb_id")
            if res:
                key = "tv_results" if media_type == "show" else "movie_results"
                items = res.get(key, [])
                if items:
                    return items[0]["id"], f"find-imdb-{val}"

    # 4. Fallback: title+year search
    endpoint = "/search/tv" if media_type == "show" else "/search/movie"
    year_param = "first_air_date_year" if media_type == "show" else "primary_release_year"
    results = None
    if year:
        r = tmdb(endpoint, query=title, **{year_param: str(year)})
        if r: results = r.get("results", [])
    if not results:
        r = tmdb(endpoint, query=title)
        if r: results = r.get("results", [])
    if not results: return None, None

    # Fuzzy match to avoid Brass-Eye-vs-Dawson trap
    name_field = "name" if media_type == "show" else "title"
    orig_field = "original_name" if media_type == "show" else "original_title"

    def norm(s): return "".join(c.lower() for c in (s or "") if c.isalnum())
    na = norm(title)

    def match(c):
        nb, nc = norm(c.get(name_field, "")), norm(c.get(orig_field, ""))
        if na == nb or na == nc: return True
        if len(na) >= 4 and (na in nb or nb in na): return True
        if difflib.SequenceMatcher(None, na, nb).ratio() >= 0.82: return True
        return False

    candidates = [c for c in results if match(c)]
    if not candidates: return None, None
    return candidates[0]["id"], "search-fuzzy"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", type=int, required=True)
    ap.add_argument("--type", choices=["show", "movie"], default="show")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--cooldown", type=int, default=20)
    ap.add_argument("--throttle", type=float, default=0.25, help="Delay between items")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log", default="/tmp/backfill-artwork.log")
    args = ap.parse_args()

    plex_type_num = "2" if args.type == "show" else "1"
    tag = "Directory" if args.type == "show" else "Video"

    logf = open(args.log, "w")
    def log(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        logf.write(line + "\n"); logf.flush()

    log(f"=== external backfill — section {args.section} ({args.type}) ===")
    log(f"    batch={args.batch} cooldown={args.cooldown}s throttle={args.throttle}s")

    # Fetch with GUID children for external-ID resolution
    code, body = plex_req(f"/library/sections/{args.section}/all?includeGuids=1", timeout=240)
    if code != 200: sys.exit(f"Failed to fetch section: HTTP {code}")
    r = ET.fromstring(body)
    items = r.findall(tag)
    log(f"Total: {len(items)}")

    # Find items needing any fix
    targets = []
    for it in items:
        thumb = (it.get("thumb", "") or "").strip()
        art = (it.get("art", "") or "").strip()
        summary = (it.get("summary", "") or "").strip()
        need_p = not thumb
        need_a = not art
        need_s = len(summary) < 30
        if need_p or need_a or need_s:
            targets.append({
                "rk": it.get("ratingKey"),
                "title": it.get("title", ""),
                "year": it.get("year", ""),
                "ids": extract_ids(it),
                "need_p": need_p, "need_a": need_a, "need_s": need_s,
            })

    log(f"Needing fix: {len(targets)}")
    log(f"  need poster:  {sum(1 for t in targets if t['need_p'])}")
    log(f"  need art:     {sum(1 for t in targets if t['need_a'])}")
    log(f"  need summary: {sum(1 for t in targets if t['need_s'])}")
    # Breakdown by available external IDs
    with_tmdb = sum(1 for t in targets if any(s == "tmdb" for s, _ in t["ids"]))
    with_tvdb = sum(1 for t in targets if any(s == "tvdb" for s, _ in t["ids"]))
    with_imdb = sum(1 for t in targets if any(s == "imdb" for s, _ in t["ids"]))
    no_ext = sum(1 for t in targets if not t["ids"])
    log(f"  with tmdb ID:  {with_tmdb}")
    log(f"  with tvdb ID:  {with_tvdb}")
    log(f"  with imdb ID:  {with_imdb}")
    log(f"  no external:   {no_ext} (will use search fallback)")

    if args.dry_run:
        for t in targets[:20]:
            log(f"  rk={t['rk']} {t['title']!r} ids={t['ids'][:3]}")
        log(f"\n[dry-run] exiting")
        return

    stats = {"resolved": 0, "unresolved": 0, "p_ok": 0, "a_ok": 0, "s_ok": 0,
             "direct-tmdb": 0, "find-tvdb": 0, "find-imdb": 0, "search-fuzzy": 0}
    t0 = time.time()
    batches = [targets[i:i + args.batch] for i in range(0, len(targets), args.batch)]

    for bi, batch in enumerate(batches, 1):
        log(f"\n--- Batch {bi}/{len(batches)}: {len(batch)} items ---")
        for t in batch:
            rk, title, year = t["rk"], t["title"], t["year"]

            tmdb_id, method = resolve_tmdb_id(t["ids"], title, year, args.type)
            if not tmdb_id:
                stats["unresolved"] += 1
                time.sleep(args.throttle); continue

            # Count resolution method
            short_method = method.split("-", 2)[0] + "-" + method.split("-", 2)[1] if "-" in method else method
            if short_method in stats:
                stats[short_method] += 1

            # Fetch full detail
            endpoint = "/tv" if args.type == "show" else "/movie"
            detail = tmdb(f"{endpoint}/{tmdb_id}")
            if not detail:
                stats["unresolved"] += 1
                time.sleep(args.throttle); continue

            stats["resolved"] += 1
            poster = detail.get("poster_path")
            backdrop = detail.get("backdrop_path")
            overview = (detail.get("overview", "") or "").strip()

            # Uploads
            if t["need_p"] and poster:
                pu = f"https://image.tmdb.org/t/p/original{poster}"
                code, _ = plex_req(f"/library/metadata/{rk}/posters?url={urllib.parse.quote(pu)}", method="POST")
                if code == 200: stats["p_ok"] += 1

            if t["need_a"] and backdrop:
                bu = f"https://image.tmdb.org/t/p/original{backdrop}"
                code, _ = plex_req(f"/library/metadata/{rk}/arts?url={urllib.parse.quote(bu)}", method="POST")
                if code == 200: stats["a_ok"] += 1

            if t["need_s"] and overview and len(overview) >= 30:
                params = {"type": plex_type_num, "id": str(rk),
                          "summary.value": overview[:2000], "summary.locked": "1"}
                code, _ = plex_req(f"/library/sections/{args.section}/all?{urllib.parse.urlencode(params)}",
                                   method="PUT")
                if code == 200: stats["s_ok"] += 1

            time.sleep(args.throttle)

        elapsed = (time.time() - t0) / 60
        log(f"  Batch {bi} done: {stats}  elapsed={elapsed:.1f}m")
        if bi < len(batches):
            log(f"  Cooldown {args.cooldown}s...")
            time.sleep(args.cooldown)

    log(f"\n=== DONE — elapsed {(time.time() - t0) / 60:.1f}m ===")
    for k, v in stats.items():
        log(f"  {k}: {v}")


if __name__ == "__main__":
    main()
