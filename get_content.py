#!/usr/bin/env python3
"""
Search piratebay.party for torrents.

  get_content.py                  # work through ~/.content_list.json (TV, last 6 days)
  get_content.py "Dune" --movie   # search all-time, no episode grouping
  get_content.py "Severance" --tv # search last 6 days, group by episode
"""

import argparse
import html
import http.client
import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, quote_plus
from urllib.request import urlopen, Request

log = logging.getLogger("get_content")

# --- Configuration ---
CONTENT_LIST = Path("~/.content_list.json").expanduser()
SEARCH_BASE = "https://piratebay.party/search"
DAYS_BACK = 6
# Date-filter fallback only: drop episodes more than this many episode numbers
# behind the newest one in the same season (re-uploads of old episodes).
STALE_EPISODE_GAP = 6
TRANSMISSION_HOST = "localhost"
# Seeder bonus: log-scaled, reaching the cap at ~20 seeders. Big enough that a
# well-seeded release can beat a slightly "better" one that will barely download.
SEEDER_WEIGHT = 16
SEEDER_CAP = 50
HTTP_RETRIES = 1

# Score weights. Tokens are matched against the lower-cased name with dots and
# underscores turned into spaces, so "DTS-HD.MA" matches "dts-hd ma".
CODEC_SCORES = {
    "hevc": 80, "h265": 80, "h 265": 80, "x265": 80,
    "h264": 60, "h 264": 60, "x264": 60, "avc": 40,
}
RESOLUTION_SCORES = {
    "2160p": 80, "4k": 75, "uhd": 70,
    "1080p": 60,
    "720p": 20,
}
# Most-specific tokens first so "dts-hd ma" matches before "dts-hd" before "dts".
AUDIO_SCORES = {
    "atmos":     30, "truehd":   30,
    "dts-hd ma": 25, "dts:x":    25,
    "dts-hd":    20,
    "eac3":      15, "ddp":      15, "dd+": 15,
    "dts":       10,
    "aac":        5, "ac3":       5,
}
# Uploaders with an established track record on piratebay.party.
# Split by content type so the +30 bonus only fires in the right context
# (e.g. TvTeam/EZTV should not boost results in a movie search).
TRUSTED_TV_UPLOADERS    = frozenset({"TvTeam", "EZTV"})
TRUSTED_MOVIE_UPLOADERS = frozenset({"YTS", "YTS.MX", "YTS.LT", "YTS.AG", "mkvCinemas", "Pahe.in", "FitGirl"})

_GB = 1024 ** 3


def _token_patterns(scores: dict[str, int]) -> list[tuple[re.Pattern, int]]:
    # Anchor only the start of each token: "ac3" must not match inside "eac3",
    # nor "4k" inside "24k", but trailing digits are normal ("DDP5.1", "AAC2.0").
    return [(re.compile(r"(?<![a-z0-9])" + re.escape(tok)), pts) for tok, pts in scores.items()]


_CODEC_PATTERNS      = _token_patterns(CODEC_SCORES)
_RESOLUTION_PATTERNS = _token_patterns(RESOLUTION_SCORES)
_AUDIO_PATTERNS      = _token_patterns(AUDIO_SCORES)

# ---------------------------------------------------------------------------
# HTML scraping
# ---------------------------------------------------------------------------

# Actual piratebay.party column order (confirmed from live HTML):
#   0: vertTh (category)
#   1: <a title="Details for NAME">  ← name
#   2: MM-DD HH:MM                   ← date (no year; &nbsp; between date and time)
#   3: <nobr><a href="magnet:...">   ← magnet
#   4: align=right  NNN MiB          ← size
#   5: align=right  NNN              ← seeders
#   6: align=right  NNN              ← leechers

_SIZE_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
_NBSP_RE = re.compile(r"&nbsp;|\xa0")
_DATE_CELL_RE = re.compile(
    r"<td>((?:Today|Y-day|\d{1,2}-\d{1,2}|\d+(?:&nbsp;|\s)*mins?)[^<]{1,20})</td>"
)


def _parse_date(raw: str, now: datetime | None = None) -> int:
    """Parse TPB date cell → unix timestamp, or 0 if unrecognised.

    Handles "N mins ago", "Today HH:MM", "Y-day HH:MM", "MM-DD HH:MM" (this
    year) and "MM-DD YYYY" (older uploads).
    """
    raw = _NBSP_RE.sub(" ", raw).strip()
    now = now or datetime.now()

    m = re.match(r"(\d+)\s*mins?\b", raw)
    if m:
        return int((now - timedelta(minutes=int(m.group(1)))).timestamp())

    m = re.match(r"(Today|Y-day)\s+(\d{1,2}):(\d{2})", raw)
    if m:
        base = now.date() if m.group(1) == "Today" else (now - timedelta(days=1)).date()
        h, mi = int(m.group(2)), int(m.group(3))
        return int(datetime(base.year, base.month, base.day, h, mi).timestamp())

    m = re.match(r"(\d{1,2})-(\d{1,2})\s+(\d{4})$", raw)
    if m:
        month, day, year = map(int, m.groups())
        try:
            return int(datetime(year, month, day).timestamp())
        except ValueError:
            pass

    m = re.match(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", raw)
    if m:
        month, day, hour, minute = map(int, m.groups())
        try:
            dt = datetime(now.year, month, day, hour, minute)
            if dt > now + timedelta(hours=1):
                dt = datetime(now.year - 1, month, day, hour, minute)
            return int(dt.timestamp())
        except ValueError:
            pass

    log.debug("unparsed date cell: %r", raw)
    return 0


def _parse_size(raw: str) -> int:
    raw = _NBSP_RE.sub(" ", raw).strip()
    m = re.match(r"([\d.]+)\s*(B|KiB|MiB|GiB|TiB)", raw)
    return int(float(m.group(1)) * _SIZE_UNITS[m.group(2)]) if m else 0


def _parse_rows(page: str) -> list[dict]:
    results = []
    # Split on <tr> boundaries; only keep chunks that look like torrent rows
    for chunk in re.split(r"<tr[^>]*>", page):
        if 'title="Details for' not in chunk or "magnet:" not in chunk:
            continue

        m_name = re.search(r'title="Details for ([^"]+)"', chunk)
        m_mag  = re.search(r'href="(magnet:[^"]+)"', chunk)
        if not m_name or not m_mag:
            continue

        m_date = _DATE_CELL_RE.search(chunk)
        # align=right tds: size, seeders, leechers
        right_tds = re.findall(r'<td align="right">([^<]+)</td>', chunk)
        # Uploader: last <td> containing a /user/ link
        m_user = re.search(r'<td><a href="/user/[^"]+/"[^>]*>([^<]+)</a></td>', chunk)

        results.append({
            # Attribute values are HTML-escaped: magnets carry "&amp;tr=…"
            "name":     html.unescape(m_name.group(1)),
            "magnet":   html.unescape(m_mag.group(1)),
            "added":    _parse_date(m_date.group(1)) if m_date else 0,
            "size":     _parse_size(right_tds[0]) if len(right_tds) > 0 else 0,
            "seeders":  int(right_tds[1]) if len(right_tds) > 1 and right_tds[1].isdigit() else 0,
            "leechers": int(right_tds[2]) if len(right_tds) > 2 and right_tds[2].isdigit() else 0,
            "uploader": html.unescape(m_user.group(1)) if m_user else "",
        })
    return results


def fetch_html(url: str) -> str:
    req = Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    log.debug("GET %s", url)
    for attempt in range(HTTP_RETRIES + 1):
        try:
            with urlopen(req, timeout=20) as resp:
                page = resp.read().decode("utf-8", errors="replace")
            break
        # OSError covers URLError/HTTPError and read timeouts; HTTPException
        # covers truncated responses (IncompleteRead) and the like.
        except (OSError, http.client.HTTPException) as e:
            if attempt < HTTP_RETRIES:
                log.debug("retrying after error: %s", e)
                time.sleep(2)
                continue
            print(f"  Network error: {e}")
            return ""

    if log.isEnabledFor(logging.DEBUG):
        log.debug("%d bytes received", len(page))
        snip_start = max(0, page.find("Details for") - 100)
        snip = page[snip_start:snip_start + 600] if "Details for" in page else page[:800]
        log.debug("snippet:\n%s\n", snip)
    return page


def clean_query(query: str) -> str:
    query = re.sub(r"['‘’`]", "", query)  # straight and curly apostrophes
    query = re.sub(r"[^\w\s\-]", " ", query)         # other punctuation → space
    return re.sub(r"\s+", " ", query).strip()


def search_torrents(query: str, sort: int = 3, category: int = 200) -> list[dict]:
    q = clean_query(query)
    url = f"{SEARCH_BASE}/{quote(q, safe='')}/1/{sort}/{category}"
    page = fetch_html(url)
    if not page:
        return []
    results = _parse_rows(page)
    log.debug("parser found %d row(s)", len(results))
    for r in results[:3]:
        log.debug("  %s  added=%s  seeds=%s", r["name"], r["added"], r["seeders"])
    return results


# ---------------------------------------------------------------------------
# Episode keys
# ---------------------------------------------------------------------------

EPISODE_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})(?!\d)")
KEY_RE     = re.compile(r"S(\d+)E(\d+)$")
HD_RE      = re.compile(r"2160p|4k|uhd|1080p", re.IGNORECASE)
SD_RE      = re.compile(r"720p|480p|576p",      re.IGNORECASE)


def episode_key(name: str) -> str:
    m = EPISODE_RE.search(name)
    return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}" if m else "UNKNOWN"


def ep_tuple(key: str | None) -> tuple[int, int] | None:
    """"S01E03" → (1, 3). Compare these rather than key strings, which
    misorder once episode numbers reach three digits ("E100" < "E99")."""
    m = KEY_RE.match(key or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _ep_sort_key(key: str) -> tuple[int, int, int]:
    t = ep_tuple(key)
    return (0, *t) if t else (1, 0, 0)


# ---------------------------------------------------------------------------
# TVMaze — verify which episodes actually aired this week
# ---------------------------------------------------------------------------

def _tvmaze_get(url: str):
    """GET JSON from TVMaze; None on any network or decode failure."""
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except (OSError, http.client.HTTPException, ValueError) as e:
        log.debug("TVMaze request failed: %s (%s)", url, e)
        return None


def tvmaze_aired_this_week(
    show_name: str, days_back: int, show_id: int | None = None
) -> dict[str, str] | None:
    """
    Return {episode_key: "S01E03 · Title (YYYY-MM-DD)"} for episodes that aired
    in the last `days_back` days. An empty dict means the show was found but
    nothing aired; None means the lookup failed or the show isn't on TVMaze.

    Pass show_id to skip the name search. Ambiguous titles resolve to whatever
    ranks first otherwise, which can silently point at the wrong show.
    """
    if show_id is None:
        results = _tvmaze_get(f"https://api.tvmaze.com/search/shows?q={quote_plus(show_name)}")
        if not results:
            return None
        show_id = results[0]["show"]["id"]

    episodes = _tvmaze_get(f"https://api.tvmaze.com/shows/{show_id}/episodes")
    if episodes is None:
        return None

    today = datetime.now().date()
    cutoff = today - timedelta(days=days_back)
    aired = {}
    for ep in episodes:
        # Specials have no episode number and can't be matched to S##E## names
        if ep.get("season") is None or ep.get("number") is None:
            continue
        airdate_str = ep.get("airdate") or ""
        if not airdate_str:
            continue
        try:
            airdate = datetime.strptime(airdate_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if cutoff <= airdate <= today:
            key = f"S{ep['season']:02d}E{ep['number']:02d}"
            aired[key] = f"{key} · {ep.get('name', '?')} ({airdate_str})"
    return aired


# ---------------------------------------------------------------------------
# Scoring and episode grouping
# ---------------------------------------------------------------------------

def _first_match(name: str, patterns: list[tuple[re.Pattern, int]]) -> int:
    for pattern, pts in patterns:
        if pattern.search(name):
            return pts
    return 0


def score_torrent(torrent: dict, cutoff_ts: int, *, is_tv: bool = True) -> int | None:
    if torrent["added"] < cutoff_ts:
        return None

    if not is_tv and EPISODE_RE.search(torrent["name"]):
        return None
    name = re.sub(r"[._]", " ", torrent["name"].lower())
    seeders = torrent.get("seeders", 0)

    score = (_first_match(name, _CODEC_PATTERNS)
             + _first_match(name, _RESOLUTION_PATTERNS)
             + _first_match(name, _AUDIO_PATTERNS))

    if seeders == 0 and not is_tv:
        # Dead in an all-time catalogue. TV keeps these: a fresh upload can show
        # 0 until the site refreshes its counts, and seeded rivals still outscore it.
        return None
    if seeders > 0:
        score += min(SEEDER_CAP, int(math.log(seeders + 1) * SEEDER_WEIGHT))

    trusted = TRUSTED_TV_UPLOADERS if is_tv else TRUSTED_MOVIE_UPLOADERS
    if torrent.get("uploader") in trusted:
        score += 30

    if not is_tv:
        size = torrent.get("size", 0)
        if size > 10 * _GB:
            return None
        if size >= 2 * _GB:
            # Linear 0→40 across 2–6 GB, then flat up to the 10 GB cap.
            score += int(min(size - 2 * _GB, 4 * _GB) / (4 * _GB) * 40)

    return score


def best_candidates(ep_results: list[tuple[int, dict]]) -> tuple[list[tuple[int, dict]], bool]:
    """Return (candidates, hd_only). Prefer HD; fall back to SD only if no HD exists."""
    hd = [(s, t) for s, t in ep_results if HD_RE.search(t["name"])]
    if hd:
        return hd, True
    sd = [(s, t) for s, t in ep_results if SD_RE.search(t["name"])]
    return (sd or ep_results), False


def group_by_episode(
    scored: list[tuple[int, dict]], *, drop_stale: bool = True
) -> dict[str, list[tuple[int, dict]]]:
    groups: dict[str, list] = defaultdict(list)
    for score, t in scored:
        groups[episode_key(t["name"])].append((score, t))
    grouped = {k: sorted(v, key=lambda x: x[0], reverse=True) for k, v in groups.items()}
    if drop_stale:
        grouped = _drop_stale_episodes(grouped)
    return dict(sorted(grouped.items(), key=lambda kv: _ep_sort_key(kv[0])))


def _drop_stale_episodes(
    episodes: dict[str, list[tuple[int, dict]]],
) -> dict[str, list[tuple[int, dict]]]:
    """Within each season, drop episodes far behind the latest (re-uploads of old eps)."""
    by_season: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for key in episodes:
        t = ep_tuple(key)
        if t:
            by_season[t[0]].append((t[1], key))

    keep: set[str] = set()
    for ep_list in by_season.values():
        max_ep = max(e for e, _ in ep_list)
        for ep_num, key in ep_list:
            if max_ep - ep_num < STALE_EPISODE_GAP:
                keep.add(key)

    # Always keep UNKNOWN-keyed entries (season packs, etc.)
    return {k: v for k, v in episodes.items() if k in keep or ep_tuple(k) is None}


# ---------------------------------------------------------------------------
# Transmission / display helpers
# ---------------------------------------------------------------------------

@dataclass
class Pick:
    magnet: str
    show: str | None = None     # set for TV picks so download history can be recorded
    ep_key: str | None = None
    advances: bool = False      # may move last_downloaded[show] forward once sent


def _open_magnet(magnet: str) -> bool:
    """Hand a magnet to the default app in the background (no focus steal)."""
    try:
        result = subprocess.run(["open", "-g", magnet], capture_output=True, text=True, timeout=15)
        error = (result.stdout + result.stderr).strip()
        ok = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as e:
        ok, error = False, str(e)
    if not ok:
        print(f"  Could not open: {error}")
        print(f"  Magnet: {magnet}")
    return ok


def add_to_transmission(magnets: list[str]) -> list[bool]:
    """Queue each magnet; returns a success flag per magnet."""
    results = []
    have_remote = True
    for magnet in magnets:
        if have_remote:
            try:
                result = subprocess.run(
                    ["transmission-remote", TRANSMISSION_HOST, "--add", magnet],
                    capture_output=True, text=True, timeout=30,
                )
            except FileNotFoundError:
                have_remote = False  # not in PATH — fall through to open -g
            except subprocess.TimeoutExpired:
                print("  Transmission error: transmission-remote timed out")
                results.append(False)
                continue
            else:
                output = (result.stdout + result.stderr).strip()
                if result.returncode == 0:
                    print(f"  Queued: {output}")
                else:
                    print(f"  Transmission error: {output}")
                results.append(result.returncode == 0)
                continue
        results.append(_open_magnet(magnet))
    return results


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "unknown"


def load_content_list() -> tuple[list[str], dict[str, int], dict]:
    """Load show list, TVMaze ids and download history.

    Entries may be plain strings or {"name": ..., "tvmaze_id": ...}. tvcal
    writes the second form; both are accepted. Auto-migrates plain text.
    """
    if not CONTENT_LIST.exists():
        sys.exit(
            f"Content list not found: {CONTENT_LIST}\n"
            'Create it as JSON: {"shows": ["Show Name", ...]}'
        )
    raw = CONTENT_LIST.read_text().strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        shows = [l.strip() for l in raw.splitlines() if l.strip() and not l.startswith("#")]
        data = {"shows": shows, "last_downloaded": {}}
        save_content_list(data)
        print(f"Migrated {CONTENT_LIST} to JSON format.\n")
    data.setdefault("last_downloaded", {})

    names: list[str] = []
    ids: dict[str, int] = {}
    for item in data.get("shows", []):
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and item.get("name"):
            names.append(item["name"])
            if item.get("tvmaze_id"):
                ids[item["name"]] = int(item["tvmaze_id"])
    return names, ids, data


def save_content_list(data: dict) -> None:
    """Write via a temp file and rename, so rsync/tvcal never see a partial file."""
    fd, tmp = tempfile.mkstemp(dir=CONTENT_LIST.parent, prefix=".content_list.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2) + "\n")
        if CONTENT_LIST.exists():
            os.chmod(tmp, CONTENT_LIST.stat().st_mode & 0o777)
        os.replace(tmp, CONTENT_LIST)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ~/.content_list.json is shared between this Mac and linuxvm (tvcal also
# writes it there). Rather than a background timer on either side, each
# machine's copy of this script just syncs with the other whenever it runs
# in list-driven mode - before reading and around writing - via two
# one-directional `rsync -u` passes, so whichever side has the newer mtime
# wins in both directions without needing a daemon anywhere.
#
# Auth differs per side. The Mac has no key file: ssh uses the 1Password agent
# from ~/.ssh/config, so a sync may raise a 1Password approval prompt. linuxvm
# uses this dedicated key, which the Mac's authorized_keys pins to a forced
# command (~/bin/content-list-rsync) that only allows rsync of this one file.
_SSH_KEY = Path("~/.ssh/id_content_sync").expanduser()
# Long enough to approve a 1Password prompt; ConnectTimeout bounds a dead peer.
_SYNC_TIMEOUT = 45


def _peer_host() -> str:
    try:
        hostname = subprocess.run(
            ["hostname", "-s"], capture_output=True, text=True, check=False, timeout=5
        ).stdout.strip().lower()
    except (OSError, subprocess.TimeoutExpired):
        hostname = ""
    return "ben@Ben.local" if hostname == "linuxvm" else "ben@linuxvm.local"


def sync_content_list_with_peer() -> None:
    """Best-effort two-way sync; an unreachable peer just leaves this run
    working from whatever is already on disk."""
    remote = f"{_peer_host()}:.content_list.json"
    local = str(CONTENT_LIST)
    ssh_cmd = "ssh -o ConnectTimeout=5 -o BatchMode=yes"
    if _SSH_KEY.exists():
        ssh_cmd += f" -i {_SSH_KEY} -o IdentitiesOnly=yes"
    for src, dst in ((local, remote), (remote, local)):
        try:
            result = subprocess.run(
                ["rsync", "-au", "-e", ssh_cmd, src, dst],
                capture_output=True, text=True, timeout=_SYNC_TIMEOUT, check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            log.debug("content list sync skipped: %s", e)
            return
        if result.returncode != 0:
            log.debug("content list sync failed (%s → %s): %s", src, dst, result.stderr.strip())
            return


def record_progress(picks: list[Pick], sent_ok: list[bool]) -> None:
    """Advance last_downloaded for picks that reached Transmission.

    A show stops advancing at its first pick that failed to send or was marked
    non-advancing, so an episode skipped earlier is offered again next run.
    The list is re-synced and re-read first so edits made on the peer during
    this run (e.g. by tvcal) aren't overwritten.
    """
    updates: dict[str, str] = {}
    blocked: set[str] = set()
    for pick, ok in zip(picks, sent_ok):
        if pick.show is None or pick.ep_key is None:
            continue
        if not ok or not pick.advances:
            blocked.add(pick.show)
        elif pick.show not in blocked:
            updates[pick.show] = pick.ep_key  # picks are in episode order
    if not updates:
        return

    sync_content_list_with_peer()
    _, _, data = load_content_list()
    ld = data["last_downloaded"]
    for show, key in updates.items():
        current = ep_tuple(ld.get(show))
        if current is None or ep_tuple(key) > current:
            ld[show] = key
    save_content_list(data)
    sync_content_list_with_peer()


def prompt_yes_no(question: str) -> bool:
    try:
        return input(f"  {question} [y/N] ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _display_tv(
    show: str,
    scored: list[tuple[int, dict]],
    expected: dict[str, str] | None,
    *,
    last_ep: str | None = None,
) -> list[Pick]:
    # With TVMaze, its episode list replaces the stale-episode heuristic; that
    # heuristic would wrongly drop early episodes of a whole-season release.
    episodes = group_by_episode(scored, drop_stale=expected is None)

    # UNKNOWN-keyed results (no S##E## in name) can't be matched to TVMaze or
    # tracked in last_downloaded, so they're never offered in TV mode.
    episodes = {k: v for k, v in episodes.items() if ep_tuple(k) is not None}

    last = ep_tuple(last_ep)
    if last:
        filtered = {k: v for k, v in episodes.items() if ep_tuple(k) > last}
        skipped = len(episodes) - len(filtered)
        if skipped:
            print(f"  Skipping {skipped} episode(s) already downloaded (last: {last_ep})")
            print()
        episodes = filtered

    # If TVMaze gave us a verified episode list, filter to just those keys;
    # also report any expected episodes that weren't found on TPB.
    first_gap = None
    if expected is not None:
        missing = [k for k in sorted(expected, key=_ep_sort_key) if k not in episodes]
        episodes = {k: v for k, v in episodes.items() if k in expected}
        if missing:
            first_gap = ep_tuple(missing[0])
            for k in missing:
                print(f"  ⚠ Not on TPB yet: {expected[k]}")
            print()

    if not episodes:
        print("  No matching torrents found.\n")
        return []

    ep_count = len(episodes)
    label = "episode" if ep_count == 1 else "episodes"
    src = "TVMaze-verified" if expected is not None else "date-filtered"
    print(f"  Found {len(scored)} result(s) → {ep_count} {label} ({src}).\n")

    selected = []
    blocked = False  # an earlier episode was declined
    for ep_key, ep_results in episodes.items():
        candidates, is_hd = best_candidates(ep_results)
        best_score, best = candidates[0]
        ep_label = expected.get(ep_key, ep_key) if expected else ep_key
        title = ep_label.split("·", 1)[1].strip() if "·" in ep_label else ""
        tag = f"[{ep_key}]"
        hd_note = "" if is_hd else "  ⚠ no HD found"
        print(f"  {tag} {title}  score {best_score}{hd_note}")
        uploader = best.get("uploader", "")
        print(f"    Name:  {best['name']}")
        print(f"    Added: {fmt_date(best['added'])}  "
              f"Size: {fmt_size(best['size'])}  "
              f"Seeds: {best.get('seeders', '?')}  "
              f"By: {uploader or 'unknown'}")
        if len(candidates) > 1:
            alt_score, alt = candidates[1]
            if alt_score >= best_score * 0.85:
                print(f"    Alt (score {alt_score}): {alt['name']}")
        print(f"    Magnet: {best['magnet'][:80]}…")

        if prompt_yes_no(f"Queue {tag}?"):
            advances = not blocked and (first_gap is None or ep_tuple(ep_key) < first_gap)
            if not advances:
                print("    (history not advanced: an earlier episode was skipped or isn't out yet)")
            selected.append(Pick(best["magnet"], show, ep_key, advances))
        else:
            blocked = True
        print()
    return selected


def _display_movie(scored: list[tuple[int, dict]]) -> list[Pick]:
    print(f"  Found {len(scored)} result(s). Top matches:\n")
    for rank, (sc, t) in enumerate(scored[:5], 1):
        uploader = t.get("uploader", "")
        print(f"  [{rank}] score {sc}  Seeds: {t.get('seeders', '?')}  By: {uploader or 'unknown'}")
        print(f"    Name:  {t['name']}")
        print(f"    Added: {fmt_date(t['added'])}  Size: {fmt_size(t['size'])}")
        print(f"    Magnet: {t['magnet'][:80]}…")
        if prompt_yes_no(f"Queue [{rank}]?"):
            print()
            return [Pick(t["magnet"])]
        print()
    return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _search_and_display(
    show: str, is_movie: bool, cutoff: int, *,
    last_ep: str | None = None, show_id: int | None = None
) -> list[Pick]:
    print(f"{'='*60}")
    print(f"  {show}")
    print(f"{'='*60}")

    expected = None
    if not is_movie:
        expected = tvmaze_aired_this_week(show, DAYS_BACK, show_id)
        if expected is None:
            print("  TVMaze: lookup failed or show not found — using date filter only\n")
        elif not expected:
            print(f"  TVMaze: nothing aired in the last {DAYS_BACK} days.\n")
            return []
        else:
            ordered = sorted(expected, key=_ep_sort_key)
            print(f"  TVMaze: {', '.join(expected[k] for k in ordered)}")
            last = ep_tuple(last_ep)
            expected = {k: expected[k] for k in ordered if not last or ep_tuple(k) > last}
            if not expected:
                print(f"  Already downloaded through {last_ep}.\n")
                return []
            print()

    if expected:
        # One search per aired episode, so a busy week of uploads can't push
        # the episode we want off the single results page we fetch.
        found, seen = [], set()
        for key in expected:
            for t in search_torrents(f"{show} {key}", sort=3):
                if t["magnet"] not in seen:
                    seen.add(t["magnet"])
                    found.append(t)
    else:
        found = search_torrents(show, sort=99 if is_movie else 3)

    tokens = clean_query(show).lower().split()
    matched = [t for t in found if all(tok in t["name"].lower() for tok in tokens)]
    scored = [(s, t) for t in matched
              if (s := score_torrent(t, cutoff, is_tv=not is_movie)) is not None]
    scored.sort(key=lambda x: x[0], reverse=True)

    # With a TVMaze list, fall through even when empty so missing episodes are reported.
    if not scored and not expected:
        if not found:
            print("  No results found.\n")
        elif not matched:
            print("  No results matched the query title.\n")
        else:
            window = "all time" if cutoff == 0 else f"the last {DAYS_BACK} days"
            print(f"  No results in {window}.\n")
        return []

    if is_movie:
        return _display_movie(scored)
    return _display_tv(show, scored, expected, last_ep=last_ep)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="?",
                    help=f"Title to search (omit to use {CONTENT_LIST.name})")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--tv",    action="store_true", help="Treat as TV show (last 6 days, group by episode)")
    mode.add_argument("--movie", action="store_true", help="Treat as movie (all-time, top results)")
    ap.add_argument("--debug",   action="store_true", help="Print raw HTTP/parse diagnostics")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="  [debug] %(message)s")

    tv_cutoff    = int((datetime.now() - timedelta(days=DAYS_BACK)).timestamp())
    movie_cutoff = 0   # all-time

    picks: list[Pick] = []
    list_mode = not args.query

    if not list_mode:
        is_movie = args.movie
        cutoff   = movie_cutoff if is_movie else tv_cutoff
        mode_label = "movie, all-time" if is_movie else f"TV, last {DAYS_BACK} days"
        print(f"Searching piratebay.party — {mode_label}\n")
        picks = _search_and_display(args.query, is_movie=is_movie, cutoff=cutoff)
    else:
        if args.tv or args.movie:
            ap.error("--tv / --movie only apply when a query argument is given")
        sync_content_list_with_peer()
        shows, show_ids, data = load_content_list()
        if not shows:
            sys.exit("No shows found in content list.")
        ld = data["last_downloaded"]
        print(f"Searching piratebay.party — {len(shows)} show(s), last {DAYS_BACK} days\n")
        for show in shows:
            picks.extend(_search_and_display(
                show, is_movie=False, cutoff=tv_cutoff,
                last_ep=ld.get(show), show_id=show_ids.get(show)))

    if picks:
        print(f"\nSending {len(picks)} torrent(s) to Transmission…")
        sent_ok = add_to_transmission([p.magnet for p in picks])
        if list_mode:
            record_progress(picks, sent_ok)


if __name__ == "__main__":
    main()
