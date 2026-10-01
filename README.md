# get_content

A command-line tool that finds the best available torrents for your TV shows and movies, and sends them straight to Transmission.

## What it does

- Reads a list of TV shows from `~/.content_list.json` (kept in sync with linuxvm)
- Checks **TVMaze** (free, no API key) to confirm which episodes actually aired this week
- Searches **piratebay.party** for each of those episodes
- Scores each result by codec (h265 > h264), resolution (1080p/4K preferred), and seeder count
- Filters to HD-only (falls back to SD with a warning if nothing better exists)
- Asks which episodes to download, then sends them all to Transmission at once (using `open -g` on macOS so focus isn't stolen)

## Setup

No dependencies beyond the Python standard library.

```bash
# Make it executable and put it in PATH
chmod +x get_content.py
ln -s "$(pwd)/get_content.py" ~/bin/get_content.py

# Create your show list
cat > ~/.content_list.json <<EOF
{"shows": ["Bob's Burgers", "Ghosts", "Hacks"]}
EOF
```

## Usage

```bash
# Check all shows in ~/.content_list.json for new episodes this week
get_content.py

# Search for a specific TV show (last 6 days, grouped by episode)
get_content.py "Severance" --tv

# One specific episode, any air date (a year picks between same-named series)
get_content.py "Scrubs 2026 S02E01"

# A whole season, any air date (offers a season pack first)
get_content.py "Neagley S01"

# Search for a movie (all-time, top 5 results by score)
get_content.py "Dune Part Two" --movie

# Debug HTTP/parse issues
get_content.py "Ghosts" --tv --debug

# Run the tests (offline)
python3 -m unittest
```

## How scoring works

Each torrent gets a score before being offered:

| Signal | Points |
|---|---|
| h265 / HEVC / x265 | +70 |
| h264 / x264 | +60 |
| AVC | +40 |
| Source: WEB-DL / WEB / BluRay | +30 |
| Source: WEBRip, or WEB-DL re-encoded (x264/x265 tag) | +10 |
| Source: HDTV / DCPRip | +5 |
| Foreign or multi-language (ITA, MULTi, Dual, Hindi, …) | −40 |
| Dolby Vision | +20 |
| HDR / HDR10 / HDR10+ (without DV) | +10 |
| Bitrate below the floor for its resolution (TV: TVMaze runtime; movies: assume 100 min) | −40 |
| 2160p / 4K / UHD | +75–80 |
| 1080p | +60 |
| 720p | +20 |
| Audio (movies only): Atmos / TrueHD | +30 |
| DTS-HD MA / DTS:X | +25 |
| DTS-HD | +20 |
| EAC3 / DDP / DD+ | +15 |
| DTS | +10 |
| AAC / AC3 | +5 |
| Seeders (log scale, full at ~20) | up to +50 |
| Trusted uploader (context-aware) | +30 |
| Movie size 2–6 GB (4K: 6–18 GB), then flat to the cap | up to +40 |

Results are filtered to only those whose names start with the search query (ignoring punctuation and case), so searching "Runner 2026" won't surface "The Runner 2026", and searching "Project Hail Mary" won't surface "The Project" or "Hail Caesar".

Cinema recordings (CAM, Telesync, HDTS, …) and "AI upscale" fake-4K releases are never offered.

For TV, only HD results (1080p+) are shown per episode. 720p is a fallback with a warning. For movies, results over 16 GB (30 GB for 4K) — i.e. remuxes — and results with no seeders are excluded.

## Transmission

The script tries `transmission-remote localhost --add <magnet>` first. If that's not in PATH (typical on macOS with the GUI app), it falls back to `open -g <magnet>`, which hands the link to Transmission in the background without stealing focus.

To use `transmission-remote` directly: `brew install transmission-cli`.

## Content list format

The list is stored as JSON at `~/.content_list.json`. The old plain-text format is auto-migrated on first run.

```json
{
  "shows": ["Bob's Burgers", "Ghosts", "Hacks"],
  "last_downloaded": {
    "Ghosts": "S05E22"
  }
}
```

Shows can also be `{"name": "Ghosts", "tvmaze_id": 46573}` to pin the TVMaze match.

`last_downloaded` is updated after episodes are successfully sent to Transmission, so re-running won't re-offer episodes you've already grabbed. If you skip an episode (or it isn't on TPB yet) the history stops before it, so it's offered again next time. Apostrophes (straight or curly) are stripped before searching.
