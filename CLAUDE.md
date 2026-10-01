# get_content — dev notes

Single-file Python script (`get_content.py`). No external dependencies — stdlib only.

Tests: `python3 -m unittest` (stdlib `unittest`, offline — `test_get_content.py` uses an inline HTML fixture and mocks TVMaze, prompts and the peer sync).

## Architecture

```
main()
 ├── sync_content_list_with_peer()   → rsync with linuxvm (list mode only)
 ├── load_content_list() / argparse  → shows + TVMaze ids + last_downloaded
 ├── per show: _search_and_display() → list[Pick]
 │    ├── tvmaze_aired_this_week()   → {} nothing aired (skip show) / None lookup failed
 │    ├── search_torrents()          → one search per aired episode, or one for the show
 │    ├── title filter               → drops results missing any query word
 │    ├── score_torrent()            → codec + resolution + audio + seeders + size
 │    └── _display_tv() / _display_movie()
 │         ├── group_by_episode()    → keyed by S##E##, sorted numerically
 │         │    └── _drop_stale_episodes() → date-filter fallback only
 │         └── best_candidates()    → HD-first filter per episode
 ├── add_to_transmission()          → transmission-remote or open -g; per-magnet success
 └── record_progress()              → re-sync, re-read, advance last_downloaded, save, sync
```

## HTML scraping (piratebay.party)

The site does **not** expose a JSON API. It uses standard TPB-proxy HTML. The search URL is:

```
https://piratebay.party/search/{query}/{page}/{sort}/{category}
```

- `sort=3` → newest first (used for TV, so recent low-seeder episodes aren't buried)
- `sort=99` → relevance (used for movies; sort=7 "most seeded" is broken on this mirror — ignores the query and returns global popular content)
- `category=200` → Video (all subcategories); used for both TV and movies

Only page 1 (~30 rows) is fetched. To keep that from hiding episodes, TV searches with a TVMaze episode list run one query per aired episode (`"Show S01E03"`), de-duplicated by magnet.

Parsing is regex-on-`<tr>`-chunks (not HTMLParser). The confirmed column order:

| col | content |
|-----|---------|
| 0 | category (`class="vertTh"`) |
| 1 | name — `<a title="Details for NAME">` |
| 2 | date — see below |
| 3 | magnet — `<nobr><a href="magnet:...">` |
| 4 | size — `align=right`, text like `582.97&nbsp;MiB` |
| 5 | seeders — `align=right`, integer |
| 6 | leechers — `align=right`, integer |
| 7 | uploader — `<a href="/user/USERNAME/" title="Browse USERNAME">USERNAME</a>` |

Key gotchas:
- Name, magnet and uploader are HTML attribute/text values and are passed through `html.unescape()` — magnets otherwise carry `&amp;tr=…` and Transmission loses the trackers.
- Dates use `&nbsp;` as the literal entity string (not decoded to `\xa0`) between date and time parts — both must be replaced before parsing.
- Date formats: `N mins ago`, `Today HH:MM`, `Y-day HH:MM`, `MM-DD HH:MM` (no year — assume current year, or last year if that would be in the future), `MM-DD YYYY` (older uploads). Unrecognised → `0` (logged under `--debug`), which drops the row in TV mode.
- The mirror displays most names with spaces instead of dots (`H 264`, `DDP5 1`), so scoring normalises `.`/`_` to spaces and uses space-form tokens.
- The `class="odd"` / `class="even"` row classes from classic TPB **do not exist** on this mirror.
- `fetch_html` retries once (`HTTP_RETRIES`) on `OSError`/`HTTPException` (covers read timeouts and truncated responses), then prints the error and returns `""`.

## TVMaze integration

Free API, no key required. Up to two calls per show:

1. `GET https://api.tvmaze.com/search/shows?q={name}` → take `results[0]["show"]["id"]` (skipped when the content list has a `tvmaze_id`)
2. `GET https://api.tvmaze.com/shows/{id}/episodes` → filter by `today - DAYS_BACK <= airdate <= today`; specials (`number: null`) are skipped

Returns `{episode_key: "S01E03 · Title (YYYY-MM-DD)"}`, `{}` if the show was found but nothing aired (the show is skipped without searching TPB), or `None` if the lookup failed / show not found (falls back to date filtering + `_drop_stale_episodes()`). Keep those two cases distinct — conflating them made the fallback offer re-uploads of old episodes for shows on a break.

The upper bound (`airdate <= today`) is essential — without it, future episodes appear as "Not on TPB yet" when they simply haven't aired. Expected keys ≤ `last_downloaded` are removed before searching, so already-grabbed episodes aren't reported missing.

## Episode keys

`episode_key()` → `S##E##` (episode up to 3 digits, e.g. `S01E105`) or `UNKNOWN`. **Never compare keys as strings** — `"S01E100" < "S01E99"`. Use `ep_tuple()` / `_ep_sort_key()`. `UNKNOWN` results are never offered in TV mode (they can't be matched to TVMaze or tracked).

## Stale episode filter (`_drop_stale_episodes`)

Date-filter fallback only (`group_by_episode(drop_stale=expected is None)`). With a TVMaze list it must not run: a whole-season release (E01–E10 in one week) would lose E01–E04. Groups found episodes by season and discards any episode more than `STALE_EPISODE_GAP` numbers behind the season's max.

## Scoring

```python
CODEC_SCORES      = {hevc/h265/x265: 80, h264/x264: 60, avc: 40}
RESOLUTION_SCORES = {2160p: 80, 4k: 75, uhd: 70, 1080p: 60, 720p: 20}
AUDIO_SCORES      = {atmos/truehd: 30, dts-hd ma/dts:x: 25, dts-hd: 20,
                     eac3/ddp/dd+: 15, dts: 10, aac/ac3: 5}
seeder_bonus      = min(50, log(seeders+1) * 16)   # SEEDER_CAP / SEEDER_WEIGHT; cap at ~20 seeds
zero_seeders      = movies dropped; TV kept (fresh uploads can show 0 before counts refresh)
trusted_uploader  = +30 (TV: TvTeam/EZTV; movies: YTS variants/mkvCinemas/Pahe.in/FitGirl)
movie_size_bonus  = linear 0→+40 across 2–6 GB, flat +40 from 6–10 GB
movie_size_cap    = files > 10 GB excluded (remuxes)
```

Key design decisions:
- Tokens match only at a word start (`(?<![a-z0-9])`) — `ac3` doesn't fire inside `eac3`, `4k` not inside `24k` — but may be followed by anything (`DDP5.1`, `AAC2.0`).
- Seeder bonus is log-scaled up to 50, so a well-seeded release can beat a slightly better-scored one that will barely download, while thousands of seeders can't outweigh codec + resolution.
- Tokens within a table are matched first-hit in dict order, so most-specific first ("dts-hd ma" before "dts-hd" before "dts").
- Trusted uploaders are split by context: TV uploaders don't get the bonus in movie searches and vice versa.
- HD filter (`best_candidates`) runs *after* scoring for TV — a high-seeder 720p can never beat a low-seeder 1080p.

## Title relevance filter

Results must contain every word of the cleaned query as a case-insensitive substring. `clean_query` strips straight *and* curly apostrophes (`‘’`) before turning other punctuation into spaces — otherwise `Grey’s` becomes `Grey s` and the token `s` matches everything.

## Fake-release detection

There is no name-based check. A "spaces in name → suspicious" heuristic used to exist but was removed: this mirror displays nearly every name with spaces, so it fired on almost everything. Selection relies on scoring (seeders, trusted uploaders, codec/resolution) and the uploader shown on each result.

If a check is reinstated, the best cheap signal is the site-assigned badge in the magnet cell (`vip.gif`, or classic TPB's `trusted.png`) — ~all results carry VIP, anonymous uploads show `<td><i>Anonymous</i></td>`. The next step up is the details page's file list (fakes ship `.exe`/`.scr`/`.lnk`/passworded `.rar`).

## Transmission

`add_to_transmission()` returns a success flag per magnet. Tries `transmission-remote {TRANSMISSION_HOST} --add` (30 s timeout); on `FileNotFoundError` switches to `open -g` (15 s timeout) for the rest of the batch. All picks are sent in one batch at the end.

## Content list (`~/.content_list.json`)

```json
{
  "shows": ["Bob's Burgers", {"name": "Ghosts", "tvmaze_id": 46573}],
  "last_downloaded": {"Ghosts": "S05E22"}
}
```

Shows may be strings or `{name, tvmaze_id}` (tvcal on linuxvm writes the latter). The old plain-text format is auto-migrated.

`save_content_list()` writes a temp file in the same directory and `os.replace()`s it (preserving mode), so rsync/tvcal never see a half-written file.

### Download history (`last_downloaded`)

Updated only by `record_progress()`, after `add_to_transmission()`, and only in list mode. Each TV `Pick` carries `advances`; a show's history stops advancing at its first pick that:
- came after a declined episode,
- came after a TVMaze-expected episode not yet on TPB, or
- failed to reach Transmission.

So a skipped/missing episode is offered again next run instead of being hidden forever. Nothing is written until the end of the run, so Ctrl+C never records episodes that were never sent.

### Peer sync

The file is shared with linuxvm. `sync_content_list_with_peer()` does two `rsync -au` passes (newer mtime wins) at the start of list mode, and `record_progress()` syncs + re-reads before merging its updates (max per show) and syncs again after saving, so concurrent tvcal edits aren't clobbered. Peer host is hardcoded in `_peer_host()`.
