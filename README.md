# plex-tools

Python utilities for managing a large Plex library. Built for a homelab setup (22k movies + 3k TV + 4k IPTV) running Plex + Kometa + Radarr + Sonarr on Proxmox, but most scripts work on any Plex server.

All scripts are **idempotent** — safe to re-run. They check Plex state at start and skip already-correct items.

## Scripts

### `bin/scan-artwork.py` — bulk artwork + description backfill

Scans an entire library section and fills missing posters, backdrops and summaries from TMDb. Runs in **batches of 200 with 30-second cooldowns** to respect TMDb's 40-req/10s rate limit.

```bash
PLEX_TOKEN=xxx TMDB_APIKEY=xxx python3 bin/scan-artwork.py --section 5
```

Flags:
- `--section N` — Plex library section ID (required)
- `--type show|movie` — default `show`
- `--batch 200` — items per batch
- `--cooldown 30` — seconds between batches
- `--throttle 0.7` — seconds between items within a batch
- `--dry-run` — report missing items without uploading

Features:
- Fuzzy title matching via `difflib.SequenceMatcher` (≥0.82) handles edge cases like `"CIA"` vs `"CIA: The Series"`
- Year-scoped TMDb search with fallback to unscoped
- Never touches title — only uploads posters/backdrops and locks summary
- Explicit HTTP status-code checks (no silent upload failures)

### `bin/check-missing.py` — audit report

Reports how many items in a section are missing posters, backdrops, or summaries.

```bash
PLEX_TOKEN=xxx python3 bin/check-missing.py --section 5
```

Example output:

```
Total: 3367
Missing poster:  1597
Missing art:     1640
Missing summary: 585
```

Flags:
- `--section N` — section ID
- `--show-missing` — list first 40 items with missing poster

### `bin/split-merged.py` — un-merge wrongly-combined shows

When Plex merges multiple folders into one show because filenames share a prefix (e.g. `Blindspot` + `Blindspotting` — real case) this splits them back apart.

```bash
PLEX_TOKEN=xxx python3 bin/split-merged.py 275261
```

After split, each original folder becomes its own item. Use `fix-shows.py` to re-match any items to the correct TMDb entry.

### `bin/fix-shows.py` — targeted show re-match

For specific items that matched wrongly. Supply `<rk>:<correct_tmdb_id>` pairs.

```bash
PLEX_TOKEN=xxx TMDB_APIKEY=xxx python3 bin/fix-shows.py 286888:2327 326171:93740
```

Per item it will:

1. `PUT /library/metadata/{rk}/match` — rebind the rk to the TMDb guid
2. Upload TMDb's top poster + backdrop
3. `PUT` the correct title, year and summary with `locked=1`

## Environment

```bash
export PLEX_URL=http://127.0.0.1:32400  # optional, default shown
export PLEX_TOKEN=xxx                   # from Preferences.xml or "Get Info"
export TMDB_APIKEY=xxx                  # TMDb v3 key
```

Plex token extraction:

```bash
docker exec plex sh -c 'grep -oE "PlexOnlineToken=\"[^\"]+" \
  "/config/Library/Application Support/Plex Media Server/Preferences.xml"' \
  | head -1 | cut -d\" -f2
```

## Common workflows

**Full library backfill from scratch:**

```bash
python3 bin/check-missing.py --section 5                 # see what's missing
python3 bin/scan-artwork.py --section 5 --dry-run        # preview targets
python3 bin/scan-artwork.py --section 5 > scan.log 2>&1 &
tail -f scan.log
```

**Fix a specific wrongly-merged show:**

```bash
python3 bin/split-merged.py 275261                       # split into components
# Note the new rk numbers, then:
python3 bin/fix-shows.py 326171:93740                    # match one component
```

## Lessons learned building this

1. **TMDb rate limit is 40 req / 10s.** Exceed it and TMDb **silently returns empty `results:[]`** for several minutes — no 429, just empty. Use batched requests with cooldowns. Sensor via `tmdb_found` count over time — if it stops climbing, you're throttled.
2. **Plex `/match` endpoint** takes `guid=tmdb%3A%2F%2F{id}&name=X&year=Y`. Only rebinds the agent, doesn't re-download art.
3. **`/split` then `/merge`** — clean way to undo mis-merges. Split by rk, then `PUT /library/metadata/{target_rk}/merge?ids=rk1,rk2` to combine a subset back.
4. **Explicit HTTP status code checks always.** `try/except Exception` catches real failures silently and inflates your success counts.
5. **Poster precedence:** agent-provided posters from TMDb/TVDB re-overwrite manual uploads unless you lock the field. Use `summary.locked=1`, `title.locked=1`, `year.locked=1` to pin.

## Context

These tools were built during a week-long Plex library overhaul to:

- Fix 1,126 duplicate entries caused by overlapping scan paths
- Backfill artwork for 618 movies missing posters/backdrops
- Re-match wrongly-merged IPTV shows (Blindspot + Blindspotting case)
- Push curation-essay summaries from [kometa-config](https://github.com/harkers/kometa-config) into Plex collection detail views

## License

MIT — see [LICENSE](./LICENSE).
