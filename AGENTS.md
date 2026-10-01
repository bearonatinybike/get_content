# AGENTS.md

## What this is

`get_content.py` — a CLI that finds the best torrent for TV episodes and movies on
piratebay.party and queues the chosen ones in Transmission. TV is checked against TVMaze
so only episodes that have actually aired are offered.

Single file, **stdlib only** (no third-party packages). Runs on the Mac and on `linuxvm`
from the same file.

```
get_content.py              all shows in ~/.content_list.json, episodes aired in the last 6 days
get_content.py "Show" --tv  one show, last 6 days
get_content.py "Show 2026 S02E01"   one episode, any air date (TV is the default mode)
get_content.py "Show S01"           a whole season (or "Season 1"), pack offered first
get_content.py "Movie 2026" --movie all-time, top 5 by score
--debug                     HTTP/parse diagnostics (also shows sync failures)
```

Every prompt defaults to No, so `< /dev/null` runs a search read-only — the standard way
to check what the script would pick.

## Layout

```
get_content.py        the script
test_get_content.py   offline unittest suite (python3 -m unittest)
README.md             user-facing overview
AGENTS.md             this file — all agent guidance lives here
```

There is deliberately no `CLAUDE.md`: Claude Code (v2.1.277+) reads `AGENTS.md` directly,
but only when no `CLAUDE.md`/`CLAUDE.local.md` exists in this directory or above it.
Adding one would stop this file loading unless it contains an `@AGENTS.md` import.

Not in the repo: `~/bin/content-list-rsync` on the Mac (see *Recreating the linuxvm → Mac
sync access*).

## Rules

- **Stdlib only.** No `pip install`; the script must run on a stock Python 3.12+ (linuxvm
  is 3.12, the Mac newer).
- **Run `python3 -m unittest` before every commit.** Add a test for every bug fix and
  scoring rule; the suite is offline (inline HTML fixture, mocked TVMaze, prompts, sync).
- **Check scoring changes against live searches, not just tests.** Run the regression
  titles below with `< /dev/null` before and after, and confirm no pick changes that
  wasn't intended. Tune rules on principles, not to flip one example.
- **Deploy = copy the file.** linuxvm has a plain copy at `~/bin/get_content.py` (no git
  clone). After pushing:
  ```bash
  scp get_content.py ben@linuxvm.local:bin/get_content.py
  ssh ben@linuxvm.local md5sum bin/get_content.py   # must match: md5 -q get_content.py
  ```
  SSH from the Mac goes through the 1Password agent; expect an approval prompt.
- **Never compare episode keys as strings** (`"S01E100" < "S01E99"`). Use `ep_tuple()` /
  `_ep_sort_key()`.
- **Keep "show not found" and "nothing aired" distinct** (`None` vs `{}`) — see TVMaze.
- **Content-list writes stay atomic** (`save_content_list()`: temp file + `os.replace`).
  Other programs on linuxvm read and write `~/.content_list.json` too, as does the rsync.
- **TVMaze is rate limited** (~20 requests / 10 s per IP), shared with anything else on
  the same host that calls it. get_content makes at most two calls per show; keep it that way.
- **The sync wrapper is not committed** and must not be symlinked into this OneDrive repo.

## What a good pick is

These preferences come from reviewing real results (see *Regression set*) and are what
the scoring encodes. When they conflict with a score, the score is wrong.

1. **Right title.** The release name must start with the title asked for: "Runner 2026"
   is not "The Runner 2026".
2. **A real release.** Never a cinema recording (CAM/TS/Telesync/HDTS/"HQ Pre"), an
   "AI upscale", or a dead torrent (movies). If the only results are cinema copies, the
   correct answer is *nothing*.
3. **English.** Never dubbed-only or burned-in-subtitle releases when a clean one exists.
   Multi-language releases that keep the English original are only mildly worse: Jellyfin
   user `ben` has preferred audio = English and "play default track" off, so it plays
   English whatever the file's default is. (User `Josh` has no preference and plays the
   file's default track — multi-language files can start in French/Spanish for him.)
4. **Untouched source over re-encodes.** The streaming service's own file (WEB-DL /
   scene "WEB") or a Blu-ray-sourced encode beats a group's re-encode of the WEB-DL.
5. **4K Dolby Vision is welcome.** The TV plays 2160p DV, so 2160p is preferred, DV over
   HDR10 over SDR. Jellyfin on linuxvm (2 vCPU) cannot transcode 4K or 10-bit HEVC, but it
   doesn't need to for this TV.
6. **Enough seeders to finish**, then the more seeded of near-equals. Seeders are a
   tiebreak and availability check, not a quality signal.
7. **Not over-compressed.** Small is fine only if the bitrate still holds up.

## Architecture

```
main()
 ├── sync_content_list_with_peer()    rsync with the other machine (list mode only)
 ├── load_content_list() / argparse   shows + TVMaze ids + last_downloaded
 ├── parse_query()                    "Show S02E01" / "Show S01" / plain title (ad-hoc TV)
 ├── per title: _search_and_display() → list[Pick]
 │    ├── tvmaze_episodes()           all episodes (None = lookup failed / not found)
 │    │    ├── aired_within()         {} = nothing aired → skip the show
 │    │    └── whole_seasons()        seasons dropped all at once → offer packs
 │    ├── search_torrents()           one search per pack season and per aired episode,
 │    │                               or one for the title (movies / fallback)
 │    ├── title_matches() / tv_title_matches()   name must start with the title
 │    │                               (TV: then year/country, then the S##E## code)
 │    ├── score_torrent()             see Scoring; None = never offer
 │    └── _display_tv() / _display_movie()
 │         ├── season packs first (_offer), then per-episode prompts
 │         ├── group_by_episode()     keyed by S##E##, sorted numerically
 │         │    └── _drop_stale_episodes()   date-filter fallback only
 │         └── best_candidates()      HD-first filter, then rank_key (score, seeders)
 ├── add_to_transmission()            transmission-remote or open -g; per-magnet success
 └── record_progress()                re-sync, re-read, advance last_downloaded, save, sync
```

## HTML scraping (piratebay.party)

No JSON API; standard TPB-proxy HTML. Search URL:

```
https://piratebay.party/search/{query}/{page}/{sort}/{category}
```

- `sort=3` newest first (TV, so fresh low-seeder episodes aren't buried).
- `sort=99` relevance (movies). `sort=7` "most seeded" is broken on this mirror — it
  ignores the query and returns globally popular content.
- `category=200` Video (all subcategories), for both.
- Only page 1 (~30 rows) is fetched, which is why TV searches per episode
  (`"Show S01E03"`) and per pack season (`"Show S01"`), de-duplicated by magnet.
- `/top/207` (HD movies) and `/top/201` (movies) are the site's top-100 lists — handy
  for picking test titles.

Parsing is regex on `<tr>` chunks (not HTMLParser). Column order:

| col | content |
|-----|---------|
| 0 | category (`class="vertTh"`) |
| 1 | name — `<a title="Details for NAME">` (links are absolute URLs) |
| 2 | date — see below |
| 3 | magnet — `<nobr><a href="magnet:...">`, then `vip.gif` if the uploader is VIP |
| 4 | size — `align=right`, e.g. `582.97&nbsp;MiB` |
| 5 | seeders — `align=right` |
| 6 | leechers — `align=right` |
| 7 | uploader — `<a href="/user/NAME/">NAME</a>`, or `<i>Anonymous</i>` |

Gotchas:
- Dates contain the literal entity `&nbsp;`. Formats: `N mins ago`, `Today HH:MM`,
  `Y-day HH:MM`, `MM-DD HH:MM` (no year: current year, or last year if that would be
  in the future), `MM-DD YYYY` (older). Unrecognised → `0` (logged with `--debug`),
  which drops the row in TV mode.
- Name/magnet/uploader go through `html.unescape()`. This mirror's magnets use raw `&`
  already; names can contain entities (`&#039;`).
- Most names are displayed with spaces instead of dots (`H 264`, `DDP5 1`) — the mirror
  does this, not the uploaders. Scoring normalises `.`/`_` to spaces.
- No `class="odd"/"even"` rows (classic TPB has them).
- Details pages (`/torrent/{id}/…`) often carry an NFO with MediaInfo: duration,
  codec, bitrate, `Writing library: x265`. Not used by the script; the way to settle
  questions a name can't (re-encode or not, which film).
- `fetch_html` retries once on `OSError`/`HTTPException` (timeouts, truncated reads),
  then prints the error and returns `""`.

## TVMaze

Free API, no key. Up to two calls per show:

1. `tvmaze_find_show()` — `GET /search/shows?q={name}`, skipped when the content list
   has a `tvmaze_id`. A trailing year (`Scrubs 2026`, `Scrubs (2001)`) is stripped from
   the query (TVMaze finds nothing with it left in) and picks the result that premiered
   that year; otherwise `results[0]`, which for ambiguous names can be the wrong show.
2. `tvmaze_episodes()` — `GET /shows/{id}/episodes` → `{key: Aired(label, runtime,
   airdate)}` for every numbered episode; specials (`number: null`) are skipped.

`tvmaze_episodes()` returning **`None`** (lookup failed / not found) falls back to date
filtering + `_drop_stale_episodes()`. **`aired_within()` returning `{}`** (found, nothing
aired) skips the show without searching. Conflating these made the fallback offer
re-uploads of old episodes for shows on a break.

`aired_within()` keeps `today - DAYS_BACK <= airdate <= today`; the upper bound stops
future episodes showing as "Not on TPB yet". Expected keys ≤ `last_downloaded` are
removed before searching. Episodes that aired today often aren't uploaded yet (e.g.
American Horror Story S13E04–06) — "Not on TPB yet" is the correct output.

TVMaze runtimes are scheduled slot lengths (30 for a ~22-minute sitcom), which matters
for the bitrate floor.

## Query forms

- **Episode** — `"Scrubs 2026 S02E01"`: `parse_query()` splits off the `S##E##`; the
  episode is looked up at any air date, searched as `"{show} {key}"` with no date window,
  and its runtime feeds the bitrate check. Same-named series need the year: without it
  "Scrubs" resolves to TVMaze's top hit and both series' releases can land under one key.
- **Season** — `"Neagley S01"` or `"Neagley Season 1"`: every aired episode of the
  season is expected, any date; a pack is offered first. When a plain show query finds
  nothing in the window, the script prints the latest episode and suggests this form
  (binge drops older than 6 days are otherwise invisible).

## Season packs

`pack_season(name)` recognises single-season packs (`S01` / `Season 1`, no episode code;
`S01-S03` isn't one). Packs are offered — before that season's episode prompts — only
for seasons in `packs`:
- season queries, and
- `whole_seasons()` in weekly mode: seasons with >1 episode where *every* TVMaze episode
  is in the expected list (post-`last_downloaded`), i.e. dropped all at once in the window.

A pack's runtime for the bitrate floor is the sum of its episodes'. Taking a pack queues
one `Pick` whose `ep_key` is the season's last episode and skips that season's episode
prompts; declining falls through to per-episode prompts. `record_progress` takes the max
key per show because pack picks precede episode picks.

Why: for Neagley S01 (8 episodes, one day) per-episode picks mixed 2160p DV (E01–E05,
no 4K release for E06–E08) and 1080p — 34 GB across 8 prompts — while the right answer
was one consistent 4K DV season pack.

## Episode keys

`episode_key()` → `S##E##` (episode up to 3 digits, e.g. `S01E105`) or `UNKNOWN`.
`UNKNOWN` results are never offered in TV mode (can't be matched to TVMaze or tracked).

## Stale episode filter

`_drop_stale_episodes()` runs only in the date-filter fallback
(`group_by_episode(drop_stale=expected is None)`): it drops episodes more than
`STALE_EPISODE_GAP` numbers behind the season's newest. With a TVMaze list it must not
run — a whole-season release (E01–E10 in a week) would lose E01–E04.

## Title matching

`title_matches(name, title)`: the name must **start with** the title's words, after
stripping a leading site tag (`[TGx]`, `www.site.org -`, `【site】`), comparing whole
words with case, apostrophes (straight and curly) and other punctuation ignored — so
`Spider-Man` matches `Spider.Man`, `Grey’s` matches `Greys`, `Bob's` matches `Bobs`.

Looser rules all failed on real searches:
- substring: "Toy Story 5" matched "Toy Story 4 … 5.1", "Dune" matched "Dunes";
- phrase anywhere: "Runner 2026" matched **The** Runner 2026 (a different film, which
  then won), "Dune" matched "Car S O S … VW Dune Buggy", "Ghosts" matched 23 other titles.

Checked across 20 earlier searches: the start anchor removed only other titles, never a
release of the one searched.

**TV is stricter** (`tv_title_matches()`): after the title come at most a year
(`19xx`/`20xx`) and/or a country tag (`US UK AU NZ CA`), then the season/episode code
(`S01E07`, `S01`, `Season 1`) — the shape every TV release name has (`Lanterns.2026.S01E07`,
`Ghosts.US.S05E01`, `Neagley (2026) S01`). This stops short titles matching longer ones:
"War" vs "War of the Worlds S01E01". Checked on 21 TV searches: picks identical; the only
releases removed were films (Severance 2006, Ghosts of Mars), other titles, and packs the
script never offers (multi-season, episode ranges).

`clean_query()` (for the search URL) strips straight and curly apostrophes before turning
other punctuation into spaces; otherwise `Grey’s` → `Grey s` and the token `s` matches
everything.

## Scoring

`score_torrent()` returns a score, or `None` for "never offer". Names are lower-cased,
`.`/`_` → spaces; tokens match at a word start (`(?<![a-z0-9])`), and within a table the
first hit in dict order wins (most specific first).

```
Dropped (None)
  cinema recording   CINEMA_RECORDING_WORDS as whole words: cam camrip hdcam ts hdts telesync
                     tc hdtc telecine pre predvd predvdrip; or telesync/telecine/hdcam/camrip
                     run into another tag ("TELESYNCx264")
  AI upscale         upscale / upscaled / upscaling
  movie: episode     name has an S##E## code
  movie: 0 seeders   dead in an all-time catalogue (TV keeps 0: fresh uploads show 0 at first)
  movie: too big     > 30 GB at 2160p, > 16 GB otherwise (remuxes)
  outside window     added before the cutoff (TV weekly: 6 days)

Points
  codec              hevc/h265/x265 70 · h264/x264 60 · avc 40
  resolution         2160p 80 · 4k 75 · uhd 70 · 1080p 60 · 720p 20
  source             web-dl/webdl/web 30 · bluray/blu-ray 30 · webrip 10 · brrip/bdrip 10
                     · hdtv 5 · dcprip/dcp 5          (whole words)
                     WEB/WEB-DL + x264/x265 tag = the group's re-encode → 10
  dynamic range      Dolby Vision (dv, dovi, "dolby vision") +20, else hdr/hdr10/hdr10plus +10
  audio (movies)     atmos/truehd 30 · dts-hd ma/dts:x 25 · dts-hd 20 · eac3/ddp/dd+ 15
                     · dts 10 · aac/ac3 5
  language           −40 dubbed or burned-in subs (DUBBED_OR_BURNED_IN_WORDS: dub dubbed hc
                     vostfr vost swesub), or a foreign language with no English
                     (FOREIGN_LANGUAGE_WORDS: ita hindi hin tamil french truefrench fre vf2 vff
                     vfq german ger deu spanish esp spa latino lat rus ukr)
                     −15 extra tracks beside English (MULTI_LANGUAGE_WORDS: multi multidub dual
                     daul nordic; or a foreign language word plus eng/english)
  seeders            min(50, log(seeders+1) × 16)  — full at ~20 seeders
  trusted uploader   +30  (TV: TvTeam, EZTV; movies: YTS variants, mkvCinemas, Pahe.in, FitGirl)
  over-compressed    −40 if size ÷ runtime < MIN_MBPS (Mbit/s, HEVC / H.264):
                     2160p 6 / 12 · 1080p 1.8 / 3.0 · 720p 0.9 / 1.5
                     runtime: TVMaze's for TV (packs: sum of episodes); movies assume 100 min
  movie size         +0→40 linear then flat to the cap: 2160p 6→18 GB, otherwise 2→6 GB
Ranking              rank_key = (score, seeders); TV then filters HD-first (best_candidates)
```

Why each rule exists (the example that drove it is in the *Regression set*):

- **Source outranks codec.** At 1080p almost every HEVC release is a re-encode of the
  service's H.264 file, so it can't be better than it. HEVC keeps only +10, as a tiebreak
  for space.
- **`x264`/`x265` = encoded by the group; `H264`/`H265`/`HEVC` alone can be untouched.**
  Groups keep `WEB-DL` in the name of their own encode (`1080P ATVP WEB-DL … X265
  POOTLED`, QxR, BONE); the encoder tag gives it away. At 2160p services stream HEVC, so
  `2160p … WEB-DL … H265` is usually the untouched file.
- **Audio ignored for TV.** Every WEB release of an episode carries the stream's audio;
  scene names (CAKES) often omit it, so audio tags only rewarded groups that write them
  down — enough to push re-encodes above the untouched file. Movies keep audio: lossless
  Blu-ray tracks vs re-encoded AAC is a real difference.
- **Language is a penalty, not a drop** — it can be the only decent copy. Dubs and
  burned-in subtitles can't be fixed at playback (−40); extra audio tracks next to the
  English original can (−15), because Jellyfin picks the preferred audio language
  (Obsession 2026: the only DV 4K copy is `MULTi.FRE.LAT` and is the right pick). Whole
  words of the name, so a title containing one (e.g. a show called "Dual …") is hit too.
- **Cinema recordings and upscales are drops.** Before a film's digital release they're
  all there is and they scored ~200 on resolution + seeders, topping the list. DCPRip
  (leaked cinema digital master) is real picture and stays at +5.
- **Seeders**: log-scaled to 50 so a well-seeded release beats a marginally "better" one
  that will barely download (Catwoman: a 0-seed torrent used to rank third), while
  thousands of seeders can't beat codec + resolution.
- **Bitrate floors** sit a little under true thresholds because TVMaze runtimes are slot
  lengths. Movies have no runtime source; 100 min is a middle value.
- **Movie size caps** exclude remuxes (25–80 GB) but keep untouched 1080p WEB-DLs
  (10–15 GB) and 4K WEB-DLs/encodes (15–28 GB).
- **HD filter after scoring** for TV: a high-seeder 720p never beats a low-seeder 1080p.

## Release-name field guide

What the names on this mirror mean in practice (observed 2026-10-01):

| Name / group | What it is |
|---|---|
| `WEB` / `WEB-DL` + `H264`/`h264` — CAKES, ETHEL, GRACE, FLUX, BYNDR, BTM, playWEB, SCOPE, KyoGo, RDNYB | Untouched service file. The default pick. |
| `ATVP` `AMZN` `DSNP` `HMAX` `iT` `MA` `PMTP` | Source service (Apple TV+, Amazon, Disney+, HBO Max, iTunes, Movies Anywhere, Paramount+) |
| `x265` — MeGusta, ELiTE, NeoNoir, POOTLED, BONE, PSA, Rapta, iVy | Group re-encodes. MeGusta 1080p is often ~1–1.5 Mbit/s (visibly squeezed); ELiTE ~2.5–3 |
| QxR (`… r00t`, `Silence`, `Ghost`, `Celdra`) | High-quality x265 encodes, often from UHD Blu-ray with lossless audio — a good movie pick |
| `[1080p] [WEBRip] [5.1]` (surferbroadband) | YTS-style small encodes; huge seeder counts, low bitrate |
| TBK (`ENG.ITA`), MIRCrew (`iTA EnG`), V3SP4EV3R, `DUAL` (KyoGo, PlayfulDarkness), Mang0z4 (`DUAL ESP-ENG`), `NORDiC`, `MULTi`/`VF2` (SUPPLY, FRQC), `[Ukr Dub]` | Foreign / dual-language |
| DKS, SyncUP, SPLiCE, YG⭐ `Telesync`/`CAM`/`HDTS`/`HQ Pre` | Cinema recordings |
| Mescwalrus `-Mesc` `Ai Upscale` | Fake 4K/8K upscales |
| `REMUX` (BTM, QuickIO, FraMe) | Disc remuxes, 30–80 GB — excluded by the size caps |
| `… (NOT The Chris Nolan FILM)` | Uploaders flagging a same-named different film |

Other mirror findings:
- Nearly every result carries the VIP badge (`vip.gif`); a few uploaders don't, and
  `Anonymous` uploads can still be VIP. Uploader names are shown on every result line.
- `EXTRG`, `TvTeam`, `jajaja`, `.BONE.`, `Freddy1714` are re-posting accounts, not groups;
  the group is the suffix of the name.

## Fake-release detection

No name-based check. A "spaces in name → suspicious" heuristic was removed: this mirror
shows nearly every name with spaces, so it fired on everything. If a check is reinstated,
the cheapest signal is the VIP badge (above); the next is the details page's file list
(fakes ship `.exe`/`.scr`/`.lnk`/passworded `.rar`).

## Transmission

`add_to_transmission()` returns a success flag per magnet. Tries `transmission-remote
{TRANSMISSION_HOST} --add` (30 s timeout); on `FileNotFoundError` switches to `open -g`
(15 s, macOS, no focus steal) for the rest of the batch. All picks go in one batch at the
end of the run.

## Content list (`~/.content_list.json`)

```json
{
  "shows": ["Bob's Burgers", {"name": "Ghosts", "tvmaze_id": 46573}],
  "last_downloaded": {"Ghosts": "S05E22"}
}
```

Shows are strings or `{name, tvmaze_id}` (other writers on linuxvm use the latter; ids
skip the TVMaze name search). The old plain-text format is auto-migrated. Writes are atomic and keep the
file's mode.

### Download history (`last_downloaded`)

Updated only by `record_progress()`, after `add_to_transmission()`, and only in list
mode. Each TV `Pick` carries `advances`; a show's history stops at its first pick that:
- came after a declined episode,
- came after a TVMaze-expected episode not yet on TPB, or
- failed to reach Transmission.

So a skipped or missing episode is offered again next run instead of hidden forever.
Nothing is written until the end of the run, so Ctrl+C never records unsent episodes.

### Peer sync

The file is shared between the Mac and linuxvm. `sync_content_list_with_peer()` does two
`rsync -au` passes (newer mtime wins) at the start of list mode; `record_progress()`
syncs and re-reads before merging its updates (max per show), saves, and syncs again, so
edits made on the other machine during a run aren't clobbered. Peer host is hardcoded in `_peer_host()`
(`ben@Ben.local` from linuxvm, `ben@linuxvm.local` otherwise). Failures are silent except
under `--debug`.

Until 2026-10-01 the sync never ran on either machine: it bailed out unless
`~/.ssh/id_ben_ed25519` existed as a file, and that key lives in 1Password. The lists had
drifted (linuxvm had 16 more shows).

- **Mac → linuxvm:** no key file; ssh uses the 1Password agent from `~/.ssh/config`, so a
  sync can raise an approval prompt (`_SYNC_TIMEOUT` = 45 s allows for it).
- **linuxvm → Mac:** passphrase-less `~/.ssh/id_content_sync` on linuxvm. The Mac's
  `authorized_keys` pins it with
  `command="/Users/ben/bin/content-list-rsync",from="192.168.1.2",restrict`; the wrapper
  only execs `/usr/bin/rsync --server [--sender] -<flags> . .content_list.json`. The Mac's
  rsync is Apple's openrsync; linuxvm's is rsync 3.2.

### Recreating the linuxvm → Mac sync access

The wrapper lives only at `~/bin/content-list-rsync` on the Mac, as a real file — the
repo is in OneDrive's CloudStorage folder, which sshd may be blocked from reading.

**1. On linuxvm** — create the key (skip if `~/.ssh/id_content_sync` exists) and trust
the Mac's host key:

```bash
ssh-keygen -t ed25519 -N "" -C content-list-sync@linuxvm -f ~/.ssh/id_content_sync
ssh-keyscan -t ed25519 Ben.local >> ~/.ssh/known_hosts
```

Before trusting it, check the scanned fingerprint (`ssh-keygen -lf <(ssh-keyscan -t
ed25519 Ben.local)`) matches `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the Mac.

**2. On the Mac** — Remote Login must be on (System Settings → General → Sharing).
Create `~/bin/content-list-rsync` with exactly this content, then `chmod 755` it:

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

**3. On the Mac** — append to `~/.ssh/authorized_keys`, substituting linuxvm's
`~/.ssh/id_content_sync.pub` and linuxvm's LAN IP for `from=`:

```
command="/Users/ben/bin/content-list-rsync",from="192.168.1.2",restrict ssh-ed25519 AAAA… content-list-sync@linuxvm
```

**4. Verify from linuxvm** — the first command succeeds; the other two must print
`content-list-rsync: command not allowed`:

```bash
S="ssh -i ~/.ssh/id_content_sync -o IdentitiesOnly=yes -o BatchMode=yes"
rsync -au -e "$S" ben@Ben.local:.content_list.json /tmp/mac_content_list.json
$S ben@Ben.local id
rsync -a -e "$S" ben@Ben.local:.ssh/authorized_keys /tmp/x
```

If an rsync upgrade breaks the sync, see what it sends with a fake ssh and adjust the
wrapper's regex:

```bash
printf '#!/bin/sh\nshift; echo "$*" >&2; exit 1\n' > /tmp/fakessh && chmod +x /tmp/fakessh
rsync -au -e /tmp/fakessh ~/.content_list.json ben@Ben.local:.content_list.json
```

## Regression set

Live searches reviewed on 2026-10-01, with the pick the script must make (and why). The
site changes, so re-check the reasoning rather than expecting identical names forever.
Run each with `< /dev/null`.

| Query | Expected pick | Rule it guards |
|---|---|---|
| `"Catwoman 2004" --movie` | an alive 1080p BluRay/WEB encode; never a 0-seed torrent | zero-seed drop, seeder weight |
| `"Scrubs 2026 S02E01"` | `1080p WEB h264-ETHEL` over ELiTE/MeGusta x265 | source over codec, bitrate floor, year picks series |
| `"Neagley S01"` | `S01 2160p AMZN WEB-DL … DV HDR10Plus H265-KRATOS` pack | season packs, DV |
| `"Neagley"` | nothing new; suggests `"Neagley S01"` | binge-drop hint |
| `"Slow Horses"` (S06E03) | `1080p WEB H264-CAKES` over TBK `ENG.ITA` and POOTLED | foreign penalty, TV audio ignored, WEB-DL+x265 = encode |
| `"Ted Lasso"` (S04E09) | `1080p WEB H264-CAKES` | same |
| `"South Park"`, `"The Simpsons"`, `"Futurama"`, `"Last Week Tonight with John Oliver"` | CAKES 1080p WEB | untouched WEB default |
| `"It's Always Sunny in Philadelphia"` | `1080p WEB h264-ETHEL` | apostrophes |
| `"Saturday Night Live"` | `1080p WEB h264-GRACE`; the Mesc "Ai Upscale" dropped | upscale drop |
| `"Lanterns"` (S01E07) | `2160p AMZN WEB-DL DV HDR10+ … BTM` | 4K DV preferred |
| `"American Horror Story"` (E04–06 aired today) | nothing; "Not on TPB yet" | same-day episodes |
| `"Toy Story 5" --movie` | `2160p iT WEB-DL DV HDR10+ … BTM`; no Toy Story 4 | 4K size cap, title anchor |
| `"Runner 2026" --movie` | `Runner.2026.1080p.AMZN.WEB-DL.DDP5.1.H264.MP4-BTM`; no "The Runner" | title anchored at start |
| `"Project Hail Mary" --movie` | `2160p iT WEB-DL … DV HDR … BYNDR` over a tied no-HDR copy | DV/HDR bonus |
| `"Coyote vs. Acme" --movie` | `2160p iT WEB-DL DV HDR10+` (BTM or BYNDR) | CAM/DCP leaks below |
| `"Supergirl 2026" --movie` | QxR `2160p UHD BluRay x265 DV HDR10+ TrueHD Atmos` | Blu-ray-sourced encode; 2.4 GB "2160p" penalised |
| `"The Mandalorian and Grogu" --movie` | QxR `2160p BluRay x265 DV HDR Atmos` | leading "The" |
| `"Masters of the Universe 2026" --movie` | `2160p AMZN WEB-DL … DV HDR … FLUX` | `TELESYNCx264` dropped, VOSTFR penalised |
| `"Disclosure Day 2026" --movie` | `2160p iT WEB-DL DV HDR10+ … BTM` | seeder tiebreak vs QxR |
| `"The Death of Robin Hood 2026" --movie` | `SDR 2160p AMZN WEB-DL … SCOPE` (no HDR version exists) | `HC` penalised |
| `"The Amazing Race"`, `"The Ark"` | RAWR `1080p AMZN WEB-DL … H 264` | untouched WEB default |
| `"War"` (S01E01 aired today) | nothing; never "War of the Worlds" | TV title needs the code next |
| `"Mutiny 2026" --movie` | `2160p iT WEB-DL DDP5.1 HDR H265-BYNDR` (no DV version exists) | HDR bonus |
| `"Backrooms 2026" --movie` | `2160p iT WEB-DL DV HDR10+ … BTM` (QxR UHD BluRay equally good) | DV 4K |
| `"Obsession 2026" --movie` | `2160p WEB-DL UNRATED DV HDR10+ MULTi FRE LAT … BTM` | multi-language −15, not −40 |
| `"Spider-Man: Brand New Day" --movie` | **nothing** — still in cinemas | "HQ Pre" = cinema recording |
| `"The Odyssey 2026" --movie` | **nothing** (Nolan's film is cinema-only) — but the script offers Kitsune | known limitation |

## Known limitations

- **Same title, same year, different film.** "The Odyssey 2026" has a low-budget film of
  that name on Amazon/Tubi (86 min per its NFO) alongside Nolan's (~3 h, cinema-only);
  the script offers the former. Names can't tell them apart; runtime from the details
  page could.
- **Ambiguous TV names without an id** resolve to TVMaze's top hit. Use a year in the
  query or a `tvmaze_id` in the content list.
- **Foreign words in a real title** would be penalised (whole-word match on the name).
- **Movie bitrate floor assumes 100 minutes**, so a short film's small encode can be
  penalised and a long film's let through.
- **TVMaze calls use `User-Agent: Mozilla/5.0`**; TVMaze asks for a descriptive one.
