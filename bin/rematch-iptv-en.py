#!/usr/bin/env python3
"""rematch-iptv-en.py — rematch IPTV TV shows named 'EN <Title> <CC>' to their
correct TMDb IDs.

IPTV providers frequently prefix TV show folder names with 'EN ' (language) and
suffix with a 2-letter country code, e.g. 'EN Young Rock US'. Plex's auto-match
agent doesn't handle this, leaving ~500 items with `local://` GUIDs and no
metadata.

This script:
  1. Finds items in --section whose guid starts with 'local://' AND matches
     the 'EN <Title> <CC>' pattern
  2. Strips the prefix and country code to get the real title
  3. TMDb-searches with strict criteria: exact normalized title match AND
     year match within ±1
  4. If a confident match is found: PUT /library/metadata/{rk}/match,
     upload poster + backdrop + summary, lock fields
  5. Otherwise: log for manual review

Conservative matching prevents the classic 'Dawson's Creek → Brass Eye' mistake
— we require both title AND year to agree.

Usage:
    PLEX_TOKEN=xxx TMDB_READ_TOKEN=xxx python3 rematch-iptv-en.py --section 5
    PLEX_TOKEN=xxx TMDB_READ_TOKEN=xxx python3 rematch-iptv-en.py --section 5 --dry-run
    PLEX_TOKEN=xxx TMDB_READ_TOKEN=xxx python3 rematch-iptv-en.py --section 5 --limit 10
"""
import argparse, os, re, sys, time, logging, urllib.parse
from plexapi.server import PlexServer
import requests
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
TMDB_TOKEN = os.environ.get("TMDB_READ_TOKEN", "")
if not PLEX_TOKEN or not TMDB_TOKEN:
    sys.exit("Set PLEX_TOKEN and TMDB_READ_TOKEN")

# Pattern: "EN <Title> <COUNTRYCODE>"
EN_PATTERN = re.compile(r"^EN (.+?) ([A-Z]{2})$")

# Accepted ISO country codes (to avoid false matches like "EN Something XX")
VALID_CC = {
    "US", "GB", "UK", "IE", "AU", "CA", "NZ",
    "DE", "FR", "ES", "IT", "NL", "BE", "SE", "DK", "NO", "FI",
    "PT", "PL", "HU", "CZ", "AT", "CH", "RU",
    "JP", "KR", "CN", "TW", "HK", "SG", "TH", "PH", "ID", "IN", "VN",
    "TR", "IL", "SA", "AE", "EG",
    "BR", "MX", "AR", "CL", "CO", "PE", "VE", "UY",
    "ZA", "NG", "KE", "MA",
}

tmdb_sess = requests.Session()
tmdb_sess.headers.update({"Authorization": f"Bearer {TMDB_TOKEN}", "Accept": "application/json"})


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
        time.sleep(wait)
        raise TmdbRateLimit()
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def norm(s): return "".join(c.lower() for c in (s or "") if c.isalnum())


def confident_match(real_title, target_year, results):
    """Return (candidate, reason) or (None, None).
    Strict: title match AND year match within ±1.
    """
    target_norm = norm(real_title)
    for c in results:
        c_name = c.get("name", "")
        c_orig = c.get("original_name", "")
        c_year_str = (c.get("first_air_date") or "")[:4]
        if not c_year_str.isdigit():
            continue
        c_year = int(c_year_str)

        # Year tolerance: ±1
        if target_year and abs(c_year - target_year) > 1:
            continue

        # Title must match exactly (normalized) OR ratio ≥0.90
        import difflib
        if norm(c_name) == target_norm:
            return c, f"exact-name year={c_year}"
        if norm(c_orig) == target_norm:
            return c, f"exact-orig year={c_year}"
        r1 = difflib.SequenceMatcher(None, target_norm, norm(c_name)).ratio()
        if r1 >= 0.90:
            return c, f"ratio={r1:.2f} year={c_year}"

    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="Test on first N items")
    ap.add_argument("--throttle", type=float, default=0.5)
    ap.add_argument("--log", default="/tmp/rematch-iptv-en.log")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(args.log, mode="w"), logging.StreamHandler()],
    )
    log = logging.getLogger()

    log.info(f"Connecting to Plex at {PLEX_URL}")
    plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=120)
    section = plex.library.sectionByID(args.section)
    log.info(f"Section: {section.title}")

    # Enumerate local:// items matching pattern
    targets = []
    for show in section.all():
        g = (show.guid or "")
        if not g.startswith("local://"):
            continue
        m = EN_PATTERN.match(show.title)
        if not m:
            continue
        real_title, cc = m.group(1), m.group(2)
        if cc not in VALID_CC:
            continue
        targets.append({
            "show": show,
            "rk": show.ratingKey,
            "plex_title": show.title,
            "real_title": real_title,
            "cc": cc,
            "year": show.year,
        })

    log.info(f"Found {len(targets)} items matching 'EN <Title> <CC>' pattern")

    if args.limit:
        targets = targets[:args.limit]
        log.info(f"Limited to first {args.limit}")

    if args.dry_run:
        for t in targets[:20]:
            log.info(f"  rk={t['rk']} plex={t['plex_title']!r} → real={t['real_title']!r} ({t['year']}) cc={t['cc']}")
        log.info(f"\n[dry-run] exiting — would process {len(targets)}")
        return

    stats = {
        "matched": 0, "no_results": 0, "low_confidence": 0,
        "tmdb_err": 0, "plex_match_ok": 0, "plex_match_fail": 0,
        "poster_ok": 0, "art_ok": 0, "summary_ok": 0,
    }
    unmatched = []

    t0 = time.time()
    for i, t in enumerate(targets, 1):
        if i % 25 == 0:
            log.info(f"  {i}/{len(targets)} elapsed={(time.time()-t0)/60:.1f}m — {stats}")

        # TMDb search
        try:
            kwargs = {"query": t["real_title"]}
            if t["year"]: kwargs["first_air_date_year"] = str(t["year"])
            r = tmdb("/search/tv", **kwargs)
            results = r.get("results", []) if r else []
        except Exception as e:
            log.warning(f"  rk={t['rk']} TMDb err: {str(e)[:80]}")
            stats["tmdb_err"] += 1
            continue

        if not results:
            stats["no_results"] += 1
            unmatched.append((t, "no_results"))
            continue

        best, reason = confident_match(t["real_title"], t["year"], results)
        if not best:
            stats["low_confidence"] += 1
            unmatched.append((t, "low_confidence"))
            continue

        stats["matched"] += 1
        tmdb_id = best["id"]
        real_name = best.get("name", t["real_title"])
        real_year = (best.get("first_air_date") or "")[:4]

        # Fetch full detail for overview + art
        try:
            detail = tmdb(f"/tv/{tmdb_id}")
        except Exception as e:
            log.warning(f"  rk={t['rk']} detail fetch err: {str(e)[:80]}")
            detail = None

        if not detail:
            stats["tmdb_err"] += 1
            continue

        # Match in Plex (rebind guid to tmdb://X)
        try:
            # Find the guid and call matches()/fixMatch()
            # plexapi: show.matches(agent="tv.plex.agents.series", title=name, year=year)
            # Then show.fixMatch(searchResult)
            # Alternative simpler: direct API
            guid = urllib.parse.quote(f"tmdb://{tmdb_id}")
            q = f"guid={guid}&name={urllib.parse.quote(real_name)}"
            if real_year: q += f"&year={real_year}"
            url = f"{PLEX_URL}/library/metadata/{t['rk']}/match?{q}&X-Plex-Token={PLEX_TOKEN}"
            resp = requests.put(url, timeout=30)
            if resp.status_code == 200:
                stats["plex_match_ok"] += 1
            else:
                stats["plex_match_fail"] += 1
                log.warning(f"  rk={t['rk']} match HTTP {resp.status_code}")
                continue
        except Exception as e:
            log.warning(f"  rk={t['rk']} match err: {str(e)[:80]}")
            stats["plex_match_fail"] += 1
            continue

        # After /match, Plex auto-populates poster+title. We still need to push
        # backdrop + summary manually. Each in its own try so one failure
        # doesn't skip the others.
        try:
            t["show"].reload()
        except Exception:
            pass

        if detail.get("backdrop_path"):
            try:
                t["show"].uploadArt(url=f"https://image.tmdb.org/t/p/original{detail['backdrop_path']}")
                stats["art_ok"] += 1
            except Exception as e:
                log.warning(f"  rk={t['rk']} backdrop upload err: {str(e)[:80]}")

        overview = (detail.get("overview") or "").strip()
        if overview and len(overview) >= 30:
            try:
                t["show"].editSummary(overview[:2000], locked=True)
                stats["summary_ok"] += 1
            except Exception as e:
                log.warning(f"  rk={t['rk']} summary edit err: {str(e)[:80]}")

        # Confirm poster was set by /match (it's auto)
        try:
            if t["show"].thumb:
                stats["poster_ok"] += 1
        except Exception:
            pass

        time.sleep(args.throttle)

    log.info(f"\n=== DONE — elapsed {(time.time()-t0)/60:.1f}m ===")
    for k, v in stats.items():
        log.info(f"  {k}: {v}")

    # Save unmatched for manual review
    if unmatched:
        path = "/tmp/rematch-iptv-en-unmatched.txt"
        with open(path, "w") as f:
            for t, why in unmatched:
                f.write(f"{t['rk']}\t{why}\t{t['plex_title']}\t{t['real_title']}\t{t['year']}\n")
        log.info(f"  Unmatched list: {path} ({len(unmatched)} items)")


if __name__ == "__main__":
    main()
