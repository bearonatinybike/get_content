# get_content — dev notes

Single-file Python script (`get_content.py`). No external dependencies — stdlib only.

## Architecture

```
main()
 ├── load_content_list() / argparse  → shows + last_downloaded dict
 ├── per show: _search_and_display()
 │    ├── tvmaze_aired_this_week()   → verified episode list (or None)
 │    ├── search_torrents()          → piratebay.party HTML scrape
 │    ├── title filter               → drops results missing any query word
 │    ├── score_torrent()            → codec + resolution + audio + seeders + size
 │    ├── group_by_episode()         → keyed by S##E##
 │    │    └── _drop_stale_episodes() → removes re-uploads of old eps
 │    └── _display_tv() / _display_movie()
 │         └── best_candidates()    → HD-first filter per episode
 ├── save_content_list()             → writes updated last_downloaded after each show
 └── add_to_transmission()          → transmission-remote or open -g
```

## HTML scraping (piratebay.party)

The site does **not** expose a JSON API. It uses standard TPB-proxy HTML. The search URL is:

```
https://piratebay.party/search/{query}/{page}/{sort}/{category}
```

- `sort=3` → newest first (used for TV, so recent low-seeder episodes aren't buried)
- `sort=99` → relevance (used for movies; sort=7 "most seeded" is broken on this mirror — ignores the query and returns global popular content)
- `category=200` → Video (all subcategories); used for both TV and movies

Parsing is regex-on-`<tr>`-chunks (not HTMLParser). The confirmed column order:

| col | content |
|-----|---------|
| 0 | category (`class="vertTh"`) |
| 1 | name — `<a title="Details for NAME">` |
| 2 | date — `Today HH:MM`, `Y-day HH:MM`, or `MM-DD HH:MM` (no year!) |
| 3 | magnet — `<nobr><a href="magnet:...">` |
| 4 | size — `align=right`, text like `582.97&nbsp;MiB` |
| 5 | seeders — `align=right`, integer |
| 6 | leechers — `align=right`, integer |
| 7 | uploader — `<a href="/user/USERNAME/" title="Browse USERNAME">USERNAME</a>` |

Key gotchas:
- Dates use `&nbsp;` as the literal entity string (not decoded to `\xa0`) between date and time parts — both must be replaced before parsing.
- Date has no year. Assume current year; if the resulting datetime would be in the future, use last year.
- `Today` / `Y-day` prefixes are used for very recent uploads.
- The `class="odd"` / `class="even"` row classes from classic TPB **do not exist** on this mirror.

## TVMaze integration

Free API, no key required. Two calls per show:

1. `GET https://api.tvmaze.com/search/shows?q={name}` → take `results[0]["show"]["id"]`
2. `GET https://api.tvmaze.com/shows/{id}/episodes` → filter by `today - DAYS_BACK <= airdate <= today`

Returns `{episode_key: "S01E03 · Title (YYYY-MM-DD)"}` or `None` if the show isn't found.

The upper bound (`airdate <= today`) is essential — without it, future episodes appear as "Not on TPB yet" when they simply haven't aired. When TVMaze data is available it replaces both the date-window filter and the stale-episode heuristic — only TVMaze-confirmed episodes are shown. If TVMaze fails, falls back to date filtering + `_drop_stale_episodes()`.

## Stale episode filter (`_drop_stale_episodes`)

Fallback for when TVMaze doesn't know the show. Groups found episodes by season, finds the max episode number per season, and discards any episode more than `DAYS_BACK` numbers behind the max. Prevents re-uploads of e.g. S16E01 from appearing when S16E14/E15 are the current episodes.

`UNKNOWN`-keyed results (no S##E## pattern in the name) are dropped entirely in date-filter fallback mode — they can't be tracked in `last_downloaded` and `"UNKNOWN" > "S##E##"` lexicographically, so they would bypass the already-downloaded filter.

## Scoring

```python
CODEC_SCORES      = {hevc/h265/x265: 120, h264/x264/h 264: 40-60, avc: 40}
RESOLUTION_SCORES = {2160p/4k/uhd: 75-80, 1080p: 60, 720p: 20}
AUDIO_SCORES      = {atmos/truehd: 30, dts-hd ma/dts:x: 25, dts-hd: 20,
                     eac3/ddp/dd+: 15, dts: 10, aac/ac3: 5}
seeder_bonus      = min(20, log(seeders+1) * 10)   # tiebreaker only
trusted_uploader  = +30 (TV: TvTeam/EZTV; movies: YTS variants/mkvCinemas/Pahe.in/FitGirl)
movie_size_bonus  = linear 0→+40 for files in the 2–6 GB sweet spot
movie_size_cap    = files > 10 GB excluded (remuxes)
```

Key design decisions:
- Seeder bonus is capped at 20 so quality signals (codec/resolution/audio) dominate. Seeds are a tiebreaker, not a quality indicator.
- Audio tokens are matched most-specific-first (dict insertion order) so "dts-hd ma" matches before "dts-hd" before "dts", and "eac3" before "ac3".
- Codec tokens include space-separated variants ("h 265", "h 264") because some uploaders write them that way.
- Trusted uploaders are split by context: TV uploaders (TvTeam, EZTV) don't get the bonus in movie searches and vice versa.
- HD filter (`best_candidates`) runs *after* scoring for TV — it filters the per-episode candidate list to 1080p+ before picking the winner, so a high-seeder 720p can never beat a low-seeder 1080p.

## Title relevance filter

Before scoring, results are hard-filtered to only those whose names contain every word of the search query as a substring (case-insensitive). This prevents e.g. "The Project 2018" appearing in a "Project Hail Mary" search. The check uses plain `tok in name.lower()` — no word splitting — which is more robust than set-membership against unexpected name formats.

Applied in `_search_and_display` before `score_torrent` is called.

## Safety checks

`_name_looks_suspicious(name, uploader)` warns when a torrent name contains spaces after stripping `[tracker]` annotations — legitimate scene/P2P releases use dots throughout. The warning is suppressed for uploaders in `TRUSTED_UPLOADERS` (they legitimately use spaces). When triggered, a "Queue anyway?" prompt gates the main queue prompt so the user is never silently bypassed.

**TV only** — the spaces heuristic is not applied in movie mode. Movie uploaders commonly use spaces in names legitimately.

The uploader is displayed on every result line (`By: USERNAME`) so it's always visible when making a decision.

## Transmission

Tries `transmission-remote {TRANSMISSION_HOST} --add {magnet}` first.  
Falls back to `open -g {magnet}` (macOS) on `FileNotFoundError` — the `-g` flag opens without activating the app, so focus stays in the terminal.

All selected magnets are queued during the review pass and sent in a single batch at the end.

## Content list (`~/.content_list.json`)

JSON file with show names and per-show download history:

```json
{
  "shows": ["Bob's Burgers", "Ghosts"],
  "last_downloaded": {
    "Ghosts": "S05E22"
  }
}
```

`load_content_list()` auto-migrates the old plain-text one-show-per-line format on first run. `save_content_list()` is called after each show in list mode so a Ctrl+C mid-run doesn't lose earlier updates.

`_display_tv()` filters out any episode key `≤ last_downloaded[show]` before prompting. The comparison is lexicographic on zero-padded `S##E##` strings, which correctly orders across season boundaries (e.g. `S03E01 > S02E15`). When the user queues an episode, `last_downloaded` is updated to the highest-keyed episode queued that session.
