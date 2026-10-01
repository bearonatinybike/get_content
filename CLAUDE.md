# get_content — dev notes

Single-file Python script (`get_content.py`). No external dependencies — stdlib only.

Tests: `python3 -m unittest` (stdlib `unittest`, offline — `test_get_content.py` uses an inline HTML fixture and mocks TVMaze, prompts and the peer sync).

## Architecture

```
main()
 ├── sync_content_list_with_peer()   → rsync with linuxvm (list mode only)
 ├── load_content_list() / argparse  → shows + TVMaze ids + last_downloaded
 ├── per show: _search_and_display() → list[Pick]
 │    ├── tvmaze_episodes()          → all episodes (None = lookup failed)
 │    │    └── aired_within()        → {} nothing aired (skip show); whole_seasons() → packs to offer
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

1. `GET https://api.tvmaze.com/search/shows?q={name}` via `tvmaze_find_show()` (skipped when the content list has a `tvmaze_id`). A trailing year (`Scrubs 2026`, `Scrubs (2001)`) is stripped from the query — TVMaze returns nothing with it left in — and used to pick the result that premiered that year; otherwise `results[0]`.
2. `GET https://api.tvmaze.com/shows/{id}/episodes` → `tvmaze_episodes()` returns every numbered episode as `{key: Aired(label, runtime, airdate)}`; specials (`number: null`) are skipped. `aired_within()` then keeps `today - DAYS_BACK <= airdate <= today`.

`aired_within()` returning `{}` means the show was found but nothing aired (the show is skipped without searching TPB), or `None` if the lookup failed / show not found (falls back to date filtering + `_drop_stale_episodes()`). Keep those two cases distinct — conflating them made the fallback offer re-uploads of old episodes for shows on a break.

The upper bound (`airdate <= today`) is essential — without it, future episodes appear as "Not on TPB yet" when they simply haven't aired. Expected keys ≤ `last_downloaded` are removed before searching, so already-grabbed episodes aren't reported missing.

## Single-episode and season queries

`get_content.py "Scrubs 2026 S02E01"` (TV mode, the default): `parse_query()` pulls the `S##E##` out of the query; the rest is the show name. That episode is looked up in `tvmaze_episodes()` (any air date), searched as `"{show} {key}"` with no date window, and treated as the expected list (so its runtime feeds the bitrate check). Without the year, same-named series (Scrubs 2001 vs 2026) resolve to TVMaze's top hit and their releases can mix under one key.

`get_content.py "Neagley S01"` (or `Season 1`): the whole season, any date — every aired episode of it is the expected list, and a season pack is offered first. When a plain show query finds nothing aired in the window, it prints the latest episode and suggests this form.

## Season packs

`pack_season(name)` recognises single-season packs (`S01` / `Season 1` with no episode code; names spanning seasons like `S01-S03` aren't packs). Packs are offered — before that season's episode prompts — only for seasons in `packs`:
- season queries (`"Show S01"`), and
- `whole_seasons()`: in list/weekly mode, seasons with >1 episode where *every* TVMaze episode of the season is in the (post-`last_downloaded`) expected list, i.e. a binge drop inside the window.

Each pack season gets one `"{show} S01"` search on top of the per-episode searches. A pack's runtime for the bitrate floor is the sum of its episodes' runtimes. Taking a pack queues one `Pick` whose `ep_key` is the season's last episode (so history jumps to it) and skips that season's episode prompts; declining falls through to per-episode prompts. `record_progress` takes the max key per show, since pack picks precede episode picks.

## Episode keys

`episode_key()` → `S##E##` (episode up to 3 digits, e.g. `S01E105`) or `UNKNOWN`. **Never compare keys as strings** — `"S01E100" < "S01E99"`. Use `ep_tuple()` / `_ep_sort_key()`. `UNKNOWN` results are never offered in TV mode (they can't be matched to TVMaze or tracked).

## Stale episode filter (`_drop_stale_episodes`)

Date-filter fallback only (`group_by_episode(drop_stale=expected is None)`). With a TVMaze list it must not run: a whole-season release (E01–E10 in one week) would lose E01–E04. Groups found episodes by season and discards any episode more than `STALE_EPISODE_GAP` numbers behind the season's max.

## Scoring

```python
CODEC_SCORES      = {hevc/h265/x265: 70, h264/x264: 60, avc: 40}
SOURCE_SCORES     = {web-dl/web/bluray: 30, webrip: 10, brrip/bdrip: 10, hdtv/dcprip/dcp: 5}   # whole words
                    # WEB/WEB-DL + x264/x265 encoder tag = re-encode → WEB_ENCODE_SCORE (10)
RESOLUTION_SCORES = {2160p: 80, 4k: 75, uhd: 70, 1080p: 60, 720p: 20}
AUDIO_SCORES      = {atmos/truehd: 30, dts-hd ma/dts:x: 25, dts-hd: 20,
                     eac3/ddp/dd+: 15, dts: 10, aac/ac3: 5}            # movies only
foreign           = −40 for any FOREIGN_WORDS (ita, multi, dual, hindi, vff, nordic, …)
cinema recordings = dropped: any CINEMA_RECORDING_WORDS (cam, ts, hdts, telesync, tc, …)
seeder_bonus      = min(50, log(seeders+1) * 16)   # SEEDER_CAP / SEEDER_WEIGHT; cap at ~20 seeds
zero_seeders      = movies dropped; TV kept (fresh uploads can show 0 before counts refresh)
trusted_uploader  = +30 (TV: TvTeam/EZTV; movies: YTS variants/mkvCinemas/Pahe.in/FitGirl)
movie_size_bonus  = linear 0→+40 then flat, per MOVIE_SIZE_GB: 2160p 6→18 GB, else 2→6 GB
movie_size_cap    = 2160p > 30 GB, else > 10 GB excluded (remuxes)
overcompressed    = −40 when size / TVMaze runtime is under MIN_MBPS for the resolution
                    (1080p: HEVC 1.8 / H.264 3.0 Mbit/s; 720p 0.9/1.5; 2160p 6/12)
ties              = broken by seeder count (rank_key)
```

Key design decisions:
- Tokens match only at a word start (`(?<![a-z0-9])`) — `ac3` doesn't fire inside `eac3`, `4k` not inside `24k` — but codec/audio tokens may be followed by anything (`DDP5.1`, `AAC2.0`). Source tokens are whole words, so `web` doesn't match `webcam`.
- HEVC gets only +10 over H.264. At 1080p most HEVC releases are re-encodes of an H.264 WEB-DL (`x265` in the name = encoded by the group; `H.265`/`HEVC` alone can be the untouched stream, common at 2160p), so they can't beat their source. Efficiency is rewarded only as a tiebreak; over-squeezed encodes (e.g. MeGusta's ~1.2 Mbit/s 1080p) are caught by the bitrate floor.
- Source outranks codec: an untouched WEB-DL beats a re-encode of it. Groups often keep `WEB-DL` in the name of their own encode (`1080P ATVP WEB-DL … X265 POOTLED`, QxR, BONE); the `x264`/`x265` encoder tag is what gives it away, so that combination scores as a rip.
- Audio is ignored for TV: every WEB release of an episode carries the stream's own audio, and scene names (e.g. CAKES) often omit it, so tags only rewarded groups that write them down. For movies it still separates lossless Blu-ray audio from re-encoded AAC.
- Foreign/multi-language releases (TBK's `ENG.ITA`, `MULTi`, `Dual`, `Hindi`, …) lose 40 rather than being dropped — the extra track is often the default, but it can still be the only decent copy. Matched as whole words of the name; a show title containing one of these words would be penalised too.
- Cinema recordings (CAM/TS/Telesync/HDTS/TC) are dropped outright: they scored ~200 on resolution + seeders and topped movie searches before the real release. DCPRip leaks (from the cinema's digital master) are proper picture and stay, at source +5.
- Not detectable from names: two different films sharing a title and year ("The Odyssey 2026 … NOT The Chris Nolan FILM"). Check the size, uploader and details page.
- The bitrate floor needs a runtime, so it only applies when TVMaze matched the episode. TVMaze runtimes are slot lengths (30 for a ~22-min sitcom), which is why the floors sit below the real thresholds.
- Seeder bonus is log-scaled up to 50, so a well-seeded release can beat a slightly better-scored one that will barely download, while thousands of seeders can't outweigh codec + resolution.
- Tokens within a table are matched first-hit in dict order, so most-specific first ("dts-hd ma" before "dts-hd" before "dts").
- Trusted uploaders are split by context: TV uploaders don't get the bonus in movie searches and vice versa.
- HD filter (`best_candidates`) runs *after* scoring for TV — a high-seeder 720p can never beat a low-seeder 1080p.

## Title relevance filter

`title_matches()`: the query's words must appear in the name as a consecutive phrase of whole words, after both are lower-cased, stripped of straight and curly apostrophes, and split on any other punctuation (so `Spider-Man` matches `Spider.Man`, `Grey’s` matches `Greys`). Looser matching was wrong: as substrings or scattered words, "Toy Story 5" matched "Toy Story 4 … 5.1". Release names lead with the title, so the phrase is always there for a real match. `clean_query` (for the search URL) likewise strips curly apostrophes — otherwise `Grey’s` becomes `Grey s`.

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

Auth (failures are silent apart from `--debug`):
- **Mac → linuxvm:** no key file; ssh uses the 1Password agent via `~/.ssh/config`, so a sync can raise a 1Password approval prompt (`_SYNC_TIMEOUT` = 45 s allows for it).
- **linuxvm → Mac:** dedicated passphrase-less key `~/.ssh/id_content_sync` on linuxvm. The Mac's `~/.ssh/authorized_keys` entry is `command="/Users/ben/bin/content-list-rsync",from="192.168.1.2",restrict`; that wrapper (deliberately not in this repo — see below) only execs `/usr/bin/rsync --server [--sender] -<flags> . .content_list.json`. If rsync's server argv changes (e.g. new flags with spaces), the wrapper's regex will need updating.

### Recreating the linuxvm → Mac sync access

The wrapper lives only at `~/bin/content-list-rsync` on the Mac. It is kept out of this repo, and must be a real file rather than a symlink into it, because the repo sits in OneDrive's CloudStorage folder, which sshd may be blocked from reading. To rebuild from scratch:

**1. On linuxvm** — create the key (skip if `~/.ssh/id_content_sync` exists) and trust the Mac's host key:

```bash
ssh-keygen -t ed25519 -N "" -C content-list-sync@linuxvm -f ~/.ssh/id_content_sync
ssh-keyscan -t ed25519 Ben.local >> ~/.ssh/known_hosts
```

Before trusting it, check the scanned fingerprint (`ssh-keygen -lf <(ssh-keyscan -t ed25519 Ben.local)`) matches `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` run on the Mac.

**2. On the Mac** — Remote Login must be on (System Settings → General → Sharing). Create `~/bin/content-list-rsync` with exactly this content, then `chmod 755 ~/bin/content-list-rsync`:

```sh
#!/bin/sh
# Forced command for linuxvm's content-list sync key (~/.ssh/authorized_keys).
# Allows only rsync of ~/.content_list.json in either direction - the two
# commands get_content.py's sync_content_list_with_peer() sends from linuxvm.
set -f
cmd=$SSH_ORIGINAL_COMMAND
if [ "$(printf '%s' "$cmd" | wc -l)" -eq 0 ] &&
   printf '%s\n' "$cmd" | grep -Eqx 'rsync --server( --sender)? -[A-Za-z.]+ \. \.content_list\.json'; then
    set -- $cmd
    shift
    exec /usr/bin/rsync "$@"
fi
echo "content-list-rsync: command not allowed: $cmd" >&2
exit 1
```

**3. On the Mac** — append to `~/.ssh/authorized_keys`, substituting linuxvm's `~/.ssh/id_content_sync.pub` and linuxvm's LAN IP for `from=`:

```
command="/Users/ben/bin/content-list-rsync",from="192.168.1.2",restrict ssh-ed25519 AAAA… content-list-sync@linuxvm
```

**4. Verify from linuxvm** — the first command should succeed; the other two must print `content-list-rsync: command not allowed`:

```bash
S="ssh -i ~/.ssh/id_content_sync -o IdentitiesOnly=yes -o BatchMode=yes"
rsync -au -e "$S" ben@Ben.local:.content_list.json /tmp/mac_content_list.json
$S ben@Ben.local id
rsync -a -e "$S" ben@Ben.local:.ssh/authorized_keys /tmp/x
```

If rsync on either side is upgraded and the sync starts failing, see what it sends with a fake ssh and adjust the wrapper's regex:

```bash
printf '#!/bin/sh\nshift; echo "$*" >&2; exit 1\n' > /tmp/fakessh && chmod +x /tmp/fakessh
rsync -au -e /tmp/fakessh ~/.content_list.json ben@Ben.local:.content_list.json
```
