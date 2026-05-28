# get_content

A command-line tool that finds the best available torrents for your TV shows and movies, and sends them straight to Transmission.

## What it does

- Reads a list of TV shows from `~/.content_list`
- Checks **TVMaze** (free, no API key) to confirm which episodes actually aired this week
- Searches **piratebay.party** for those episodes
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
cat > ~/.content_list <<EOF
# TV shows — one per line, # for comments
Bob's Burgers
Ghosts
Hacks
EOF
```

## Usage

```bash
# Check all shows in ~/.content_list for new episodes this week
get_content.py

# Search for a specific TV show (last 6 days, grouped by episode)
get_content.py "Severance" --tv

# Search for a movie (all-time, top 5 results by score)
get_content.py "Dune Part Two" --movie

# Debug HTTP/parse issues
get_content.py "Ghosts" --tv --debug
```

## How scoring works

Each torrent gets a score before being offered:

| Signal | Points |
|---|---|
| h265 / HEVC / x265 | +120 |
| h264 / x264 | +40–60 |
| 2160p / 4K / UHD | +75–80 |
| 1080p | +60 |
| 720p | +20 |
| Atmos / TrueHD | +30 |
| DTS-HD MA / DTS:X | +25 |
| DTS-HD | +20 |
| EAC3 / DDP / DD+ | +15 |
| DTS | +10 |
| AAC / AC3 | +5 |
| Seeders (log scale, tiebreaker) | up to +20 |
| Trusted uploader (context-aware) | +30 |
| Movie size 2–6 GB (quality sweet spot) | up to +40 |

Results are filtered to only those whose names contain every word of the search query, so searching "Project Hail Mary" won't surface "The Project" or "Hail Caesar".

For TV, only HD results (1080p+) are shown per episode. 720p is a fallback with a warning. For movies, results over 10 GB (remuxes) are excluded, and names with spaces trigger a warning since scene/P2P releases use dots.

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

`last_downloaded` is updated automatically as you queue episodes, so re-running won't re-offer episodes you've already grabbed. Special characters like apostrophes are stripped before searching.
