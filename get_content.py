#!/usr/bin/env python3
"""
Search piratebay.party for torrents.

  get_content.py                  # work through ~/.content_list (TV, last 6 days)
  get_content.py "Dune" --movie   # search all-time, no episode grouping
  get_content.py "Severance" --tv # search last 6 days, group by episode
"""

import argparse
import json
import math
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import urlopen, Request
from urllib.error import URLError

# --- Configuration ---
CONTENT_LIST = Path("~/.content_list.json").expanduser()
SEARCH_BASE = "https://piratebay.party/search"
DAYS_BACK = 6
TRANSMISSION_HOST = "localhost"

# Score weights
CODEC_SCORES = {
    "hevc": 120, "h265": 120, "h.265": 120, "x265": 120,
    "h264": 60,  "h.264": 60,  "x264": 60,  "avc": 40,
}
RESOLUTION_SCORES = {
    "2160p": 80, "4k": 75, "uhd": 70,
    "1080p": 60,
    "720p": 20,
}
# Uploaders with an established track record on piratebay.party; suppress
# the spaces-in-name heuristic for these since they legitimately use spaces.
TRUSTED_UPLOADERS = frozenset({
    "TvTeam", "EZTV", "YTS", "YTS.MX", "YTS.LT", "YTS.AG",
    "mkvCinemas", "Pahe.in", "FitGirl",
})

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


def _parse_date(raw: str) -> int:
    """Parse TPB date cell → unix timestamp. Handles Today/Y-day and MM-DD HH:MM."""
    raw = raw.replace("&nbsp;", " ").replace("\xa0", " ").strip()
    now = datetime.now()

    m = re.match(r"(Today|Y-day)\s+(\d{1,2}):(\d{2})", raw)
    if m:
        base = now.date() if m.group(1) == "Today" else (now - timedelta(days=1)).date()
        h, mi = int(m.group(2)), int(m.group(3))
        return int(datetime(base.year, base.month, base.day, h, mi).timestamp())

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

    return 0


def _parse_size(raw: str) -> int:
    raw = raw.replace("&nbsp;", " ").replace("\xa0", " ").strip()
    m = re.match(r"([\d.]+)\s*(B|KiB|MiB|GiB|TiB)", raw)
    return int(float(m.group(1)) * _SIZE_UNITS[m.group(2)]) if m else 0


def _parse_rows(html: str) -> list[dict]:
    results = []
    # Split on <tr> boundaries; only keep chunks that look like torrent rows
    for chunk in re.split(r"<tr[^>]*>", html):
        if 'title="Details for' not in chunk or "magnet:" not in chunk:
            continue

        m_name = re.search(r'title="Details for ([^"]+)"', chunk)
        m_mag  = re.search(r'href="(magnet:[^"]+)"', chunk)
        if not m_name or not m_mag:
            continue

        # Date cell: "Today HH:MM", "Y-day HH:MM", or "MM-DD HH:MM"
        m_date = re.search(r"<td>((?:Today|Y-day|\d{1,2}-\d{1,2})[^<]{3,15})</td>", chunk)
        # align=right tds: size, seeders, leechers
        right_tds = re.findall(r'<td align="right">([^<]+)</td>', chunk)
        # Uploader: last <td> containing a /user/ link
        m_user = re.search(r'<td><a href="/user/[^"]+/"[^>]*>([^<]+)</a></td>', chunk)

        results.append({
            "name":     m_name.group(1),
            "magnet":   m_mag.group(1),
            "added":    _parse_date(m_date.group(1)) if m_date else 0,
            "size":     _parse_size(right_tds[0]) if len(right_tds) > 0 else 0,
            "seeders":  int(right_tds[1]) if len(right_tds) > 1 and right_tds[1].isdigit() else 0,
            "leechers": int(right_tds[2]) if len(right_tds) > 2 and right_tds[2].isdigit() else 0,
            "uploader": m_user.group(1) if m_user else "",
        })
    return results


_debug = False


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
    if _debug:
        print(f"  [debug] GET {url}")
    try:
        with urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
            if _debug:
                print(f"  [debug] {len(html)} bytes received")
                snip_start = max(0, html.find("Details for") - 100)
                snip = html[snip_start:snip_start + 600] if "Details for" in html else html[:800]
                print(f"  [debug] snippet:\n{snip}\n")
            return html
    except URLError as e:
        print(f"  Network error: {e}")
        return ""


def clean_query(query: str) -> str:
    query = re.sub(r"['''`]", "", query)       # apostrophes
    query = re.sub(r"[^\w\s\-]", " ", query)   # other punctuation → space
    return re.sub(r"\s+", " ", query).strip()


def search_torrents(query: str, sort: int = 3) -> list[dict]:
    q = clean_query(query)
    url = f"{SEARCH_BASE}/{quote_plus(q)}/1/{sort}/200"
    html = fetch_html(url)
    if not html:
        return []
    results = _parse_rows(html)
    if _debug:
        print(f"  [debug] parser found {len(results)} row(s)")
        for r in results[:3]:
            print(f"  [debug]   {r['name']}  added={r['added']}  seeds={r['seeders']}")
    return results


# ---------------------------------------------------------------------------
# TVMaze — verify which episodes actually aired this week
# ---------------------------------------------------------------------------

def _tvmaze_get(url: str):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return None


def tvmaze_aired_this_week(show_name: str, days_back: int) -> dict[str, str] | None:
    """
    Return {episode_key: "S01E03 · Title (YYYY-MM-DD)"} for episodes that aired
    in the last `days_back` days, or None if the show can't be found on TVMaze.
    """
    results = _tvmaze_get(f"https://api.tvmaze.com/search/shows?q={quote_plus(show_name)}")
    if not results:
        return None
    show_id = results[0]["show"]["id"]

    episodes = _tvmaze_get(f"https://api.tvmaze.com/shows/{show_id}/episodes")
    if not episodes:
        return None

    today = datetime.now().date()
    cutoff = today - timedelta(days=days_back)
    aired = {}
    for ep in episodes:
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
    return aired or None


# ---------------------------------------------------------------------------
# Scoring and episode grouping
# ---------------------------------------------------------------------------

def score_torrent(torrent: dict, cutoff_ts: int) -> int | None:
    if torrent["added"] < cutoff_ts:
        return None

    name = torrent["name"].lower()
    seeders = torrent.get("seeders", 0)
    score = 0

    for token, pts in CODEC_SCORES.items():
        if token in name:
            score += pts
            break

    for token, pts in RESOLUTION_SCORES.items():
        if token in name:
            score += pts
            break

    if seeders > 0:
        score += min(50, int(math.log(seeders + 1) * 10))

    if torrent.get("uploader") in TRUSTED_UPLOADERS:
        score += 30

    return score


EPISODE_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})")
HD_RE      = re.compile(r"2160p|4k|uhd|1080p", re.IGNORECASE)
SD_RE      = re.compile(r"720p|480p|576p",      re.IGNORECASE)


def episode_key(name: str) -> str:
    m = EPISODE_RE.search(name)
    return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}" if m else "UNKNOWN"


def best_candidates(ep_results: list[tuple[int, dict]]) -> tuple[list[tuple[int, dict]], bool]:
    """Return (candidates, hd_only). Prefer HD; fall back to SD only if no HD exists."""
    hd = [(s, t) for s, t in ep_results if HD_RE.search(t["name"])]
    if hd:
        return hd, True
    sd = [(s, t) for s, t in ep_results if SD_RE.search(t["name"])]
    return (sd or ep_results), False


def group_by_episode(scored: list[tuple[int, dict]]) -> dict[str, list[tuple[int, dict]]]:
    groups: dict[str, list] = defaultdict(list)
    for score, t in scored:
        groups[episode_key(t["name"])].append((score, t))
    grouped = {k: sorted(v, key=lambda x: x[0], reverse=True) for k, v in groups.items()}
    return dict(sorted(_drop_stale_episodes(grouped).items()))


def _drop_stale_episodes(
    episodes: dict[str, list[tuple[int, dict]]],
) -> dict[str, list[tuple[int, dict]]]:
    """Within each season, drop episodes far behind the latest (re-uploads of old eps)."""
    KEY_RE = re.compile(r"S(\d+)E(\d+)")

    by_season: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for key in episodes:
        m = KEY_RE.match(key)
        if m:
            by_season[int(m.group(1))].append((int(m.group(2)), key))

    keep: set[str] = set()
    for ep_list in by_season.values():
        max_ep = max(e for e, _ in ep_list)
        for ep_num, key in ep_list:
            if max_ep - ep_num < DAYS_BACK:
                keep.add(key)

    # Always keep UNKNOWN-keyed entries (season packs, etc.)
    unknown = {k: v for k, v in episodes.items() if not KEY_RE.match(k)}
    return {k: v for k, v in episodes.items() if k in keep} | unknown


# Strip [tracker.tag] annotations then check for spaces (scene/P2P releases
# use dots throughout — spaces indicate a non-standard, potentially fake release).
_TRACKER_TAG_RE = re.compile(r"\[.*?\]")

def _name_looks_suspicious(name: str, uploader: str = "") -> str | None:
    """Return a warning string if the torrent name has suspicious formatting, else None."""
    if uploader in TRUSTED_UPLOADERS:
        return None
    clean = _TRACKER_TAG_RE.sub("", name).strip()
    if " " in clean:
        return "name contains spaces (scene/P2P releases use dots, not spaces)"
    return None


# ---------------------------------------------------------------------------
# Transmission / display helpers
# ---------------------------------------------------------------------------

def add_to_transmission(magnets: list[str]) -> None:
    for magnet in magnets:
        try:
            result = subprocess.run(
                ["transmission-remote", TRANSMISSION_HOST, "--add", magnet],
                capture_output=True, text=True,
            )
            output = (result.stdout + result.stderr).strip()
            if result.returncode == 0:
                print(f"  Queued: {output}")
            else:
                print(f"  Transmission error: {output}")
        except FileNotFoundError:
            # transmission-remote not in PATH — use open -g (background, no focus steal)
            result = subprocess.run(["open", "-g", magnet], capture_output=True, text=True)
            if result.returncode != 0:
                print(f"  Could not open: {(result.stdout + result.stderr).strip()}")
                print(f"  Magnet: {magnet}")


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "unknown"


def load_content_list() -> tuple[list[str], dict]:
    """Load show list and download history. Auto-migrates plain-text format to JSON."""
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
        CONTENT_LIST.write_text(json.dumps(data, indent=2) + "\n")
        print(f"Migrated {CONTENT_LIST} to JSON format.\n")
    data.setdefault("last_downloaded", {})
    return data.get("shows", []), data


def save_content_list(data: dict) -> None:
    CONTENT_LIST.write_text(json.dumps(data, indent=2) + "\n")


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
    last_downloaded: dict | None = None,
) -> list[str]:
    episodes = group_by_episode(scored)

    last_ep = (last_downloaded or {}).get(show)
    if last_ep:
        filtered = {k: v for k, v in episodes.items() if k > last_ep}
        skipped = len(episodes) - len(filtered)
        if skipped:
            print(f"  Skipping {skipped} episode(s) already downloaded (last: {last_ep})")
            print()
        episodes = filtered

    # Without TVMaze, UNKNOWN-keyed results (no S##E## in name) are unidentifiable
    # noise that can never be tracked — drop them in date-filter fallback mode.
    if not expected:
        episodes = {k: v for k, v in episodes.items() if k != "UNKNOWN"}

    # If TVMaze gave us a verified episode list, filter to just those keys;
    # also report any expected episodes that weren't found on TPB.
    if expected:
        missing = [label for key, label in sorted(expected.items()) if key not in episodes]
        episodes = {k: v for k, v in episodes.items() if k in expected}
        if missing:
            for m in missing:
                print(f"  ⚠ Not on TPB yet: {m}")
            print()

    if not episodes:
        print("  No matching torrents found.\n")
        return []

    ep_count = len(episodes)
    label = "episode" if ep_count == 1 else "episodes"
    src = "TVMaze-verified" if expected else "date-filtered"
    print(f"  Found {len(scored)} result(s) → {ep_count} {label} ({src}).\n")

    selected = []
    for ep_key, ep_results in episodes.items():
        candidates, is_hd = best_candidates(ep_results)
        best_score, best = candidates[0]
        ep_label = expected[ep_key] if expected and ep_key in expected else ep_key
        tag = f"[{ep_key}]"
        hd_note = "" if is_hd else "  ⚠ no HD found"
        print(f"  {tag} {ep_label.split('·')[-1].strip() if '·' in ep_label else ''}  score {best_score}{hd_note}")
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
        if _name_looks_suspicious(best["name"], uploader):
            print(f"  ⚠ Suspicious name: spaces detected (scene/P2P releases use dots)")
            if not prompt_yes_no("Queue anyway?"):
                print()
                continue
        if prompt_yes_no(f"Queue {tag}?"):
            selected.append(best["magnet"])
            if last_downloaded is not None:
                if show not in last_downloaded or ep_key > last_downloaded[show]:
                    last_downloaded[show] = ep_key
        print()
    return selected


def _display_movie(show: str, scored: list[tuple[int, dict]]) -> list[str]:
    print(f"  Found {len(scored)} result(s). Top matches:\n")
    selected = []
    for rank, (sc, t) in enumerate(scored[:5], 1):
        uploader = t.get("uploader", "")
        print(f"  [{rank}] score {sc}  Seeds: {t.get('seeders', '?')}  By: {uploader or 'unknown'}")
        print(f"    Name:  {t['name']}")
        print(f"    Added: {fmt_date(t['added'])}  Size: {fmt_size(t['size'])}")
        print(f"    Magnet: {t['magnet'][:80]}…")
        if _name_looks_suspicious(t["name"], uploader):
            print(f"  ⚠ Suspicious name: spaces detected (scene/P2P releases use dots)")
            if not prompt_yes_no("Queue anyway?"):
                print()
                continue
        if prompt_yes_no(f"Queue [{rank}]?"):
            selected.append(t["magnet"])
        print()
    return selected


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _search_and_display(
    show: str, is_movie: bool, cutoff: int, *, last_downloaded: dict | None = None
) -> list[str]:
    print(f"{'='*60}")
    print(f"  {show}")
    print(f"{'='*60}")

    expected = None
    if not is_movie:
        expected = tvmaze_aired_this_week(show, DAYS_BACK)
        if expected:
            print(f"  TVMaze: {', '.join(sorted(expected.values()))}")
        else:
            print("  TVMaze: show not found — using date filter only")
        print()

    raw = search_torrents(show, sort=8 if is_movie else 3)
    if not raw:
        print("  No results found.\n")
        return []

    scored = [(s, t) for t in raw if (s := score_torrent(t, cutoff)) is not None]
    if not scored:
        window = "all time" if cutoff == 0 else f"the last {DAYS_BACK} days"
        print(f"  No results in {window}.\n")
        return []

    scored.sort(key=lambda x: x[0], reverse=True)

    if is_movie:
        return _display_movie(show, scored)
    else:
        return _display_tv(show, scored, expected, last_downloaded=last_downloaded)


def main() -> None:
    global _debug
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="?",
                    help="Title to search (omit to use ~/.content_list)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--tv",    action="store_true", help="Treat as TV show (last 6 days, group by episode)")
    mode.add_argument("--movie", action="store_true", help="Treat as movie (all-time, top results)")
    ap.add_argument("--debug",   action="store_true", help="Print raw HTTP/parse diagnostics")
    args = ap.parse_args()
    _debug = args.debug

    tv_cutoff    = int((datetime.now() - timedelta(days=DAYS_BACK)).timestamp())
    movie_cutoff = 0   # all-time

    queue: list[str] = []

    if args.query:
        is_movie = args.movie
        cutoff   = movie_cutoff if is_movie else tv_cutoff
        mode_label = "movie, all-time" if is_movie else f"TV, last {DAYS_BACK} days"
        print(f"Searching piratebay.party — {mode_label}\n")
        queue = _search_and_display(args.query, is_movie=is_movie, cutoff=cutoff)
    else:
        if args.tv or args.movie:
            ap.error("--tv / --movie only apply when a query argument is given")
        shows, data = load_content_list()
        if not shows:
            sys.exit("No shows found in content list.")
        ld = data["last_downloaded"]
        print(f"Searching piratebay.party — {len(shows)} show(s), last {DAYS_BACK} days\n")
        for show in shows:
            queue.extend(_search_and_display(show, is_movie=False, cutoff=tv_cutoff, last_downloaded=ld))
            save_content_list(data)

    if queue:
        print(f"\nSending {len(queue)} torrent(s) to Transmission…")
        add_to_transmission(queue)


if __name__ == "__main__":
    main()
