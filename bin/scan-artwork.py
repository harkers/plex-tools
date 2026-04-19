#!/usr/bin/env python3
"""Batched Plex library scan — fills missing posters/backdrops/summaries from TMDb.

Usage:
    PLEX_TOKEN=xxx TMDB_APIKEY=xxx python3 scan-artwork.py --section 5

Idempotent: safe to re-run. Reads fresh Plex state at start.
Batched at 200 shows with 30s TMDb cooldown between batches.
"""
import argparse, urllib.request, urllib.parse, urllib.error, json, os, time, difflib
import xml.etree.ElementTree as ET

PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400")
TOKEN = os.environ.get("PLEX_TOKEN", "")
TMDB = os.environ.get("TMDB_APIKEY", "")

if not TOKEN or not TMDB:
    raise SystemExit("Set PLEX_TOKEN and TMDB_APIKEY env vars")


def plex_req(path, method="GET", timeout=60):
    url = f"{PLEX_URL}{path}{'&' if '?' in path else '?'}X-Plex-Token={TOKEN}"
    try:
        req = urllib.request.Request(url, method=method)
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode()


def plex_xml(path, timeout=60):
    code, body = plex_req(path, timeout=timeout)
    return ET.fromstring(body) if code == 200 and body else None


def tmdb(path, **p):
    p["api_key"] = TMDB
    url = f"https://api.themoviedb.org/3{path}?{urllib.parse.urlencode(p)}"
    for _ in range(4):
        try:
            return json.loads(urllib.request.urlopen(url, timeout=15).read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", 15))
                print(f"    429 — sleeping {wait}s"); time.sleep(wait + 2); continue
            return None
        except Exception:
            time.sleep(5)
    return None


def norm(s):
    return "".join(c.lower() for c in (s or "") if c.isalnum())


def title_match(p, t, o=""):
    a, b, c = norm(p), norm(t), norm(o)
    if not a: return False
    if a == b or a == c: return True
    if len(a) >= 4 and (a in b or b in a or (c and (a in c or c in a))): return True
    if difflib.SequenceMatcher(None, a, b).ratio() >= 0.82: return True
    if c and difflib.SequenceMatcher(None, a, c).ratio() >= 0.82: return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", type=int, required=True, help="Plex library section ID")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--cooldown", type=int, default=30)
    ap.add_argument("--throttle", type=float, default=0.7)
    ap.add_argument("--type", choices=["show", "movie"], default="show")
    ap.add_argument("--dry-run", action="store_true", help="Report only, no uploads")
    args = ap.parse_args()

    plex_type = "2" if args.type == "show" else "1"
    print(f"=== Plex library scan — section {args.section} ({args.type}) ===")

    r = plex_xml(f"/library/sections/{args.section}/all", timeout=240)
    if r is None:
        raise SystemExit("Failed to fetch library — check PLEX_TOKEN")
    tag = "Directory" if args.type == "show" else "Video"
    items = r.findall(tag)
    print(f"Total: {len(items)}")

    targets = []
    for s in items:
        thumb = (s.get("thumb", "") or "").strip()
        art = (s.get("art", "") or "").strip()
        summary = (s.get("summary", "") or "").strip()
        if not thumb or not art or len(summary) < 30:
            targets.append({
                "rk": s.get("ratingKey"),
                "title": s.get("title", ""),
                "year": s.get("year", ""),
                "need_p": not thumb,
                "need_a": not art,
                "need_s": len(summary) < 30,
            })
    print(f"Needing fix: {len(targets)}")

    if args.dry_run:
        for t in targets[:40]:
            print(f"  rk={t['rk']} {t['title']} ({t['year']}) p={t['need_p']} a={t['need_a']} s={t['need_s']}")
        print(f"\n[dry-run] exiting")
        return

    stats = {"hit": 0, "miss": 0, "p_ok": 0, "p_fail": 0, "a_ok": 0, "a_fail": 0, "s_ok": 0, "s_fail": 0}
    t0 = time.time()
    search_path = "/search/tv" if args.type == "show" else "/search/movie"
    detail_path = "/tv" if args.type == "show" else "/movie"
    year_param = "first_air_date_year" if args.type == "show" else "primary_release_year"

    batches = [targets[i:i + args.batch] for i in range(0, len(targets), args.batch)]
    for bi, batch in enumerate(batches, 1):
        print(f"\n--- Batch {bi}/{len(batches)}: {len(batch)} items ---")
        for t in batch:
            rk, title, year = t["rk"], t["title"], t["year"]

            # Search
            results = None
            if year:
                rr = tmdb(search_path, query=title, **{year_param: year})
                if rr: results = rr.get("results", [])
            if not results:
                rr = tmdb(search_path, query=title)
                if rr: results = rr.get("results", [])
            if not results:
                stats["miss"] += 1; time.sleep(args.throttle); continue

            name_field = "name" if args.type == "show" else "title"
            orig_field = "original_name" if args.type == "show" else "original_title"
            matches = [c for c in results if title_match(title, c.get(name_field, ""), c.get(orig_field, ""))]
            if not matches:
                stats["miss"] += 1; time.sleep(args.throttle); continue

            if year:
                try:
                    y = int(year)
                    date_field = "first_air_date" if args.type == "show" else "release_date"
                    matches.sort(key=lambda c: abs(int((c.get(date_field) or "0000")[:4] or 0) - y))
                except Exception: pass
            best = matches[0]
            detail = tmdb(f"{detail_path}/{best['id']}")
            if not detail:
                stats["miss"] += 1; time.sleep(args.throttle); continue
            stats["hit"] += 1

            poster = detail.get("poster_path")
            backdrop = detail.get("backdrop_path")
            overview = (detail.get("overview", "") or "").strip()

            if t["need_p"] and poster:
                code, _ = plex_req(f"/library/metadata/{rk}/posters?url={urllib.parse.quote('https://image.tmdb.org/t/p/original' + poster)}", method="POST")
                stats["p_ok" if code == 200 else "p_fail"] += 1
            if t["need_a"] and backdrop:
                code, _ = plex_req(f"/library/metadata/{rk}/arts?url={urllib.parse.quote('https://image.tmdb.org/t/p/original' + backdrop)}", method="POST")
                stats["a_ok" if code == 200 else "a_fail"] += 1
            if t["need_s"] and overview and len(overview) >= 30:
                params = {"type": plex_type, "id": str(rk),
                          "summary.value": overview[:2000], "summary.locked": "1"}
                code, _ = plex_req(f"/library/sections/{args.section}/all?{urllib.parse.urlencode(params)}", method="PUT")
                stats["s_ok" if code == 200 else "s_fail"] += 1
            time.sleep(args.throttle)

        elapsed = (time.time() - t0) / 60
        print(f"  Batch {bi} done: {stats}  elapsed={elapsed:.1f}m")
        if bi < len(batches):
            print(f"  Cooldown {args.cooldown}s...")
            time.sleep(args.cooldown)

    print(f"\n=== DONE === elapsed={(time.time() - t0) / 60:.1f}m")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
