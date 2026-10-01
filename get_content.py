#!/usr/bin/env python3
"""
Search piratebay.party for torrents.

  get_content.py                  # work through ~/.content_list.json (TV, last 6 days)
  get_content.py "Dune" --movie   # search all-time, no episode grouping
  get_content.py "Severance" --tv # search last 6 days, group by episode
  get_content.py "Scrubs 2026 S02E01"  # one episode, any date (year picks the series)
  get_content.py "Neagley S01"         # a whole season: packs first, any date
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
from datetime import date, datetime, timedelta
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
#
# HEVC only gets a small edge: it's more efficient, but most 1080p HEVC
# releases are re-encodes of an H.264 WEB-DL, so they can't be better than it.
# Over-squeezed ones are caught by MIN_MBPS instead.
CODEC_SCORES = {
    "hevc": 70, "h265": 70, "h 265": 70, "x265": 70,
    "h264": 60, "h 264": 60, "x264": 60, "avc": 40,
}
# Where the video came from: an untouched WEB-DL ("WEB" in scene naming) or a
# Blu-ray encode beats a WEBRip (re-captured/re-encoded) beats a TV capture or
# a cinema-master leak. Matched as whole words; "webrip" must precede "web".
# A WEB/WEB-DL name with an encoder tag (x264/x265) is the group's re-encode
# of the download, so it scores as WEB_ENCODE_SCORE instead.
SOURCE_SCORES = {
    "web-dl": 30, "webdl": 30, "webrip": 10, "web": 30,
    "bluray": 30, "blu-ray": 30, "brrip": 10, "bdrip": 10,
    "hdtv": 5, "dcprip": 5, "dcp": 5,
}
WEB_ENCODE_SCORE = 10
_UNTOUCHED_WEB = {"web-dl", "webdl", "web"}
# Recorded in a cinema (camcorder / line audio): never worth having once a
# real release exists, and often mislabelled, so they're dropped outright.
CINEMA_RECORDING_WORDS = frozenset({
    "cam", "camrip", "hdcam", "ts", "hdts", "telesync", "tc", "hdtc", "telecine",
    "pre", "predvd", "predvdrip",   # "HQ Pre": pre-release = recorded in a cinema
})
# Long enough to also catch when run into a neighbouring tag ("TELESYNCx264").
_CINEMA_RECORDING_SUBSTRINGS = ("telesync", "telecine", "hdcam", "camrip")
# Language. All matched as whole words of the name.
# - Can't be fixed at playback (dubbed-only audio, burned-in subtitles) or has
#   no English at all: FOREIGN_PENALTY.
# - Extra language tracks alongside the English original: MULTI_LANGUAGE_PENALTY.
#   Jellyfin plays the user's preferred audio language (English) whatever the
#   file's default track is, so these are only mildly worse.
DUBBED_OR_BURNED_IN_WORDS = frozenset({
    "dub", "dubbed",
    "hc",                    # hardcoded subtitles, usually Korean
    "vostfr", "vost",        # French subtitles, typically burned in
    "swesub",
})
FOREIGN_LANGUAGE_WORDS = frozenset({
    "ita", "hindi", "hin", "tamil", "french", "truefrench", "fre", "vf2", "vff", "vfq",
    "german", "ger", "deu", "spanish", "esp", "spa", "latino", "lat", "rus", "ukr",
})
MULTI_LANGUAGE_WORDS = frozenset({"multi", "multidub", "dual", "daul", "nordic"})
ENGLISH_WORDS = frozenset({"eng", "english"})
FOREIGN_PENALTY = 40
MULTI_LANGUAGE_PENALTY = 15
# "AI upscale" releases are SD/HD blown up to 2160p+: fake 4K, dropped.
UPSCALE_WORDS = frozenset({"upscale", "upscaled", "upscaling"})
# Dynamic range, for TVs that can show it (the target TV does Dolby Vision):
# DV beats plain HDR10/HDR10+, which beats SDR at the same resolution.
DOLBY_VISION_SCORE = 20
HDR_SCORE = 10
_DV_WORDS = frozenset({"dv", "dovi"})
_HDR_WORDS = frozenset({"hdr", "hdr10", "hdr10plus"})
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

# Minimum average bitrate (Mbit/s, whole file) per resolution as (HEVC, H.264).
# Below it, a release has been squeezed further than the codec's efficiency
# covers and has visibly lost detail. Only checked when TVMaze supplies a
# runtime; those are slot lengths (30 for a ~22-minute sitcom), so the floors
# sit a little under the real thresholds.
MIN_MBPS = {
    "2160p": (6.0, 12.0),
    "1080p": (1.8, 3.0),
    "720p":  (0.9, 1.5),
}
OVERCOMPRESSED_PENALTY = 40
# Movies have no TVMaze runtime; the bitrate floor assumes a typical length.
MOVIE_ASSUMED_RUNTIME = 100
# Movie sizes in GB as (bonus starts, bonus full, cap) per resolution: the
# bonus rises linearly to +40 then stays flat; above the cap is dropped
# (remuxes). 4K needs roughly 3x the bits of 1080p.
MOVIE_SIZE_GB = {
    "2160p": (6, 18, 30),
    "other": (2, 6, 16),   # untouched 1080p WEB-DLs run 10-15 GB; remuxes 25+
}

_GB = 1024 ** 3


def _token_patterns(
    scores: dict[str, int], *, whole_word: bool = False
) -> list[tuple[re.Pattern, int]]:
    # Anchor the start of each token: "ac3" must not match inside "eac3", nor
    # "4k" inside "24k". Codec/audio tokens may be followed by digits
    # ("DDP5.1", "AAC2.0"); whole_word tokens may not ("web" vs "webcam").
    end = r"(?![a-z0-9])" if whole_word else ""
    return [(re.compile(r"(?<![a-z0-9])" + re.escape(tok) + end), pts)
            for tok, pts in scores.items()]


_CODEC_PATTERNS      = _token_patterns(CODEC_SCORES)
_RESOLUTION_PATTERNS = _token_patterns(RESOLUTION_SCORES)
_AUDIO_PATTERNS      = _token_patterns(AUDIO_SCORES)
_SOURCE_PATTERNS     = [(re.compile(r"(?<![a-z0-9])" + re.escape(tok) + r"(?![a-z0-9])"), tok)
                        for tok in SOURCE_SCORES]
_ENCODER_RE          = re.compile(r"(?<![a-z0-9])x26[45](?![0-9])")
_HEVC_RE             = re.compile(r"(?<![a-z0-9])(?:hevc|h ?265|x265)")
_RES_CLASS_RE        = [(re.compile(r"(?<![a-z0-9])(?:2160p|4k|uhd)"), "2160p"),
                        (re.compile(r"(?<![a-z0-9])1080p"), "1080p"),
                        (re.compile(r"(?<![a-z0-9])720p"), "720p")]

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


def _words(text: str) -> str:
    """Lower-case, apostrophes dropped, other punctuation → single spaces,
    padded so whole words can be found with f" {word} " in ..."""
    text = re.sub(r"['\u2018\u2019`]", "", text.lower())
    return " " + " ".join(re.findall(r"[a-z0-9]+", text)) + " "


# Site/tracker tags some uploaders put before the title: "[TGx] ",
# "www.Torrenting.com - ", "【site】".
_NAME_PREFIX_RE = re.compile(
    r"^\s*(?:\[[^\]]*\]|【[^】]*】|www\.\S+|[a-z0-9-]+\.(?:com|org|net|to|io|cc|me))\s*[-–:|]*\s*",
    re.IGNORECASE,
)


def _strip_site_prefix(name: str) -> str:
    while (stripped := _NAME_PREFIX_RE.sub("", name, count=1)) != name:
        name = stripped
    return name


def title_matches(name: str, query: str) -> bool:
    """True if `name` starts with `query`'s words (whole words, ignoring case
    and punctuation, after any leading site tag). Release names lead with the
    title; matching anywhere let "Runner 2026" pick "The Runner 2026" and
    "Toy Story 5" match "Toy Story 4 ... 5.1"."""
    return _words(_strip_site_prefix(name)).startswith(_words(query))


# What may sit between a show's title and its season/episode code.
_TV_TITLE_SUFFIX_RE = re.compile(r"(?:19|20)\d\d|us|uk|au|nz|ca")
_TV_CODE_RE = re.compile(r"s\d{1,2}(?:e\d{1,3})?")


def tv_title_matches(name: str, show: str) -> bool:
    """title_matches(), plus the title must be followed directly by the
    season/episode code — optionally after a year or country tag
    ("Lanterns.2026.S01E07", "Ghosts.US.S05E01", "Show Season 1"). Stops a
    short title matching a longer one: "War" vs "War of the Worlds S01E01"."""
    words = _words(_strip_site_prefix(name)).split()
    title = _words(show).split()
    if words[:len(title)] != title:
        return False
    rest = words[len(title):]
    for _ in range(2):
        if rest and _TV_TITLE_SUFFIX_RE.fullmatch(rest[0]):
            rest = rest[1:]
    if not rest:
        return False
    return bool(_TV_CODE_RE.fullmatch(rest[0])) or (
        rest[0] == "season" and len(rest) > 1 and rest[1].isdigit())


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


# Whole-season packs: "Show.S01.1080p", "Show 2026 Season 1 Complete".
_SEASON_RE = re.compile(r"(?<![a-z0-9])(?:s|season[ ._]?)(\d{1,2})(?![0-9])", re.IGNORECASE)


def pack_season(name: str) -> int | None:
    """Season number if `name` is a single-season pack, else None. Names with
    an episode code, or naming several seasons ("S01-S03"), aren't packs."""
    if EPISODE_RE.search(name):
        return None
    seasons = {int(m.group(1)) for m in _SEASON_RE.finditer(name)}
    return seasons.pop() if len(seasons) == 1 else None


def parse_query(query: str) -> tuple[str, str | None, int | None]:
    """Split a TV query into (show, episode_key, season):
    "Scrubs 2026 S02E01" → ("Scrubs 2026", "S02E01", None);
    "Neagley S01" → ("Neagley", None, 1); "Severance" → ("Severance", None, None)."""
    m = EPISODE_RE.search(query)
    if not m:
        m = _SEASON_RE.search(query)
    if not m:
        return query, None, None
    show = re.sub(r"\s+", " ", query[:m.start()] + " " + query[m.end():]).strip()
    if m.re is EPISODE_RE:
        return show, episode_key(m.group(0)), None
    return show, None, int(m.group(1))


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


@dataclass
class Aired:
    label: str                   # "S01E03 · Title (YYYY-MM-DD)"
    runtime: int | None          # minutes (TVMaze's scheduled slot length)
    airdate: date | None = None


_YEAR_SUFFIX_RE = re.compile(r"^(.*\S)\s+\(?((?:19|20)\d\d)\)?$")


def tvmaze_find_show(name: str) -> int | None:
    """TVMaze id for a show name, or None.

    A trailing year ("Scrubs 2026") picks the same-named series that premiered
    that year — TVMaze's search finds nothing with the year left in. Otherwise
    TVMaze's top hit, which can silently be the wrong show for ambiguous names.
    """
    m = _YEAR_SUFFIX_RE.match(name.strip())
    query, year = (m.group(1), m.group(2)) if m else (name, None)
    results = _tvmaze_get(f"https://api.tvmaze.com/search/shows?q={quote_plus(query)}")
    if not results:
        return None
    if year:
        for r in results:
            if (r["show"].get("premiered") or "").startswith(year):
                return r["show"]["id"]
    return results[0]["show"]["id"]


def tvmaze_episodes(show_name: str, show_id: int | None = None) -> dict[str, Aired] | None:
    """Every numbered episode of the show by key, or None if the lookup failed
    or the show isn't on TVMaze. Pass show_id to skip the name search."""
    if show_id is None:
        show_id = tvmaze_find_show(show_name)
        if show_id is None:
            return None
    episodes = _tvmaze_get(f"https://api.tvmaze.com/shows/{show_id}/episodes")
    if episodes is None:
        return None
    out = {}
    for ep in episodes:
        # Specials have no episode number and can't be matched to S##E## names
        if ep.get("season") is None or ep.get("number") is None:
            continue
        key = f"S{ep['season']:02d}E{ep['number']:02d}"
        try:
            airdate = datetime.strptime(ep.get("airdate") or "", "%Y-%m-%d").date()
        except ValueError:
            airdate = None
        label = f"{key} · {ep.get('name') or '?'} ({ep.get('airdate') or 'no date'})"
        out[key] = Aired(label, ep.get("runtime"), airdate)
    return out


def aired_within(
    episodes: dict[str, Aired], days_back: int, today: date | None = None
) -> dict[str, Aired]:
    """Episodes that aired in the last `days_back` days (not future ones)."""
    today = today or datetime.now().date()
    cutoff = today - timedelta(days=days_back)
    return {k: a for k, a in episodes.items() if a.airdate and cutoff <= a.airdate <= today}


def whole_seasons(expected: dict[str, Aired], episodes: dict[str, Aired]) -> set[int]:
    """Seasons with more than one episode, all of them in `expected` — i.e.
    released all at once — so worth offering as a single season pack."""
    by_season: dict[int, set[str]] = defaultdict(set)
    for key in episodes:
        by_season[ep_tuple(key)[0]].add(key)
    return {s for s, keys in by_season.items() if len(keys) > 1 and keys <= expected.keys()}


# ---------------------------------------------------------------------------
# Scoring and episode grouping
# ---------------------------------------------------------------------------

def _first_match(name: str, patterns: list[tuple[re.Pattern, int]]) -> int:
    for pattern, pts in patterns:
        if pattern.search(name):
            return pts
    return 0


def _language_penalty(words: set[str]) -> int:
    if words & DUBBED_OR_BURNED_IN_WORDS:
        return FOREIGN_PENALTY
    if words & MULTI_LANGUAGE_WORDS or (words & FOREIGN_LANGUAGE_WORDS and words & ENGLISH_WORDS):
        return MULTI_LANGUAGE_PENALTY
    if words & FOREIGN_LANGUAGE_WORDS:
        return FOREIGN_PENALTY
    return 0


def _source_score(name: str) -> int:
    for pattern, tok in _SOURCE_PATTERNS:
        if pattern.search(name):
            if tok in _UNTOUCHED_WEB and _ENCODER_RE.search(name):
                return WEB_ENCODE_SCORE
            return SOURCE_SCORES[tok]
    return 0


def _resolution(name: str) -> str | None:
    for pattern, res in _RES_CLASS_RE:
        if pattern.search(name):
            return res
    return None


def _bitrate_floor(name: str) -> float | None:
    res = _resolution(name)
    if res is None:
        return None
    hevc, h264 = MIN_MBPS[res]
    return hevc if _HEVC_RE.search(name) else h264


def score_torrent(
    torrent: dict, cutoff_ts: int, *, is_tv: bool = True, runtime_min: int | None = None
) -> int | None:
    if torrent["added"] < cutoff_ts:
        return None

    if not is_tv and EPISODE_RE.search(torrent["name"]):
        return None
    words = set(_words(torrent["name"]).split())
    if words & (CINEMA_RECORDING_WORDS | UPSCALE_WORDS):
        return None
    if any(s in torrent["name"].lower() for s in _CINEMA_RECORDING_SUBSTRINGS):
        return None
    name = re.sub(r"[._]", " ", torrent["name"].lower())
    seeders = torrent.get("seeders", 0)

    score = (_first_match(name, _CODEC_PATTERNS)
             + _first_match(name, _RESOLUTION_PATTERNS)
             + _source_score(name))
    # Every WEB release of an episode carries the stream's own audio, so for TV
    # an audio tag only says the group wrote it down. Movies differ (lossless
    # Blu-ray audio vs a re-encoded AAC track), so audio counts there.
    if not is_tv:
        score += _first_match(name, _AUDIO_PATTERNS)
    score -= _language_penalty(words)
    if words & _DV_WORDS or " dolby vision " in _words(torrent["name"]):
        score += DOLBY_VISION_SCORE
    elif words & _HDR_WORDS:
        score += HDR_SCORE

    size = torrent.get("size", 0)
    floor = _bitrate_floor(name)
    if not is_tv and not runtime_min:
        runtime_min = MOVIE_ASSUMED_RUNTIME
    if runtime_min and size and floor:
        mbps = size * 8 / (runtime_min * 60) / 1e6
        if mbps < floor:
            score -= OVERCOMPRESSED_PENALTY

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
        lo, full, cap = MOVIE_SIZE_GB["2160p" if _resolution(name) == "2160p" else "other"]
        if size > cap * _GB:
            return None
        if size >= lo * _GB:
            score += int(min(size - lo * _GB, (full - lo) * _GB) / ((full - lo) * _GB) * 40)

    return score


def rank_key(scored: tuple[int, dict]) -> tuple[int, int]:
    """Sort key for (score, torrent): score, then seeders to break ties."""
    return scored[0], scored[1].get("seeders", 0)


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
    grouped = {k: sorted(v, key=rank_key, reverse=True) for k, v in groups.items()}
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
            prev = updates.get(pick.show)
            if prev is None or ep_tuple(pick.ep_key) > ep_tuple(prev):
                updates[pick.show] = pick.ep_key
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
    expected: dict[str, Aired] | None,
    *,
    last_ep: str | None = None,
    src: str = "TVMaze-verified",
    packs: set[int] = frozenset(),
) -> list[Pick]:
    # Season packs for the seasons in `packs` are offered first; any other
    # pack is noise here. Taking a pack skips that season's episode prompts.
    pack_results: dict[int, list[tuple[int, dict]]] = defaultdict(list)
    ep_scored = []
    for st in scored:
        season = pack_season(st[1]["name"])
        if season is None:
            ep_scored.append(st)
        elif season in packs:
            pack_results[season].append(st)

    selected: list[Pick] = []
    covered: set[int] = set()
    for season in sorted(pack_results):
        keys = sorted((k for k in expected or {} if ep_tuple(k)[0] == season), key=_ep_sort_key)
        candidates, is_hd = best_candidates(sorted(pack_results[season], key=rank_key, reverse=True))
        if _offer(candidates, is_hd, f"[S{season:02d} pack]", f"{len(keys) or '?'} episodes"):
            # Don't advance history past earlier-season episodes still to be offered
            earlier = any(ep_tuple(k)[0] < season and ep_tuple(k)[0] not in covered
                          for k in expected or {})
            selected.append(Pick(candidates[0][1]["magnet"], show,
                                 keys[-1] if keys else None, advances=not earlier))
            covered.add(season)
        print()
    if covered and expected is not None:
        expected = {k: a for k, a in expected.items() if ep_tuple(k)[0] not in covered}

    # With TVMaze, its episode list replaces the stale-episode heuristic; that
    # heuristic would wrongly drop early episodes of a whole-season release.
    episodes = group_by_episode(ep_scored, drop_stale=expected is None)
    episodes = {k: v for k, v in episodes.items() if ep_tuple(k) is None or ep_tuple(k)[0] not in covered}

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
                print(f"  ⚠ Not on TPB yet: {expected[k].label}")
            print()

    if not episodes:
        if not selected:
            print("  No matching torrents found.\n")
        return selected

    ep_count = len(episodes)
    label = "episode" if ep_count == 1 else "episodes"
    if expected is None:
        src = "date-filtered"
    print(f"  Found {len(scored)} result(s) → {ep_count} {label} ({src}).\n")

    blocked = False  # an earlier episode was declined
    for ep_key, ep_results in episodes.items():
        candidates, is_hd = best_candidates(ep_results)
        ep_label = expected[ep_key].label if expected and ep_key in expected else ep_key
        title = ep_label.split("·", 1)[1].strip() if "·" in ep_label else ""
        if _offer(candidates, is_hd, f"[{ep_key}]", title):
            advances = not blocked and (first_gap is None or ep_tuple(ep_key) < first_gap)
            if not advances:
                print("    (history not advanced: an earlier episode was skipped or isn't out yet)")
            selected.append(Pick(candidates[0][1]["magnet"], show, ep_key, advances))
        else:
            blocked = True
        print()
    return selected


def _offer(candidates: list[tuple[int, dict]], is_hd: bool, tag: str, title: str) -> bool:
    """Print the best candidate (and a close runner-up) and ask to queue it."""
    best_score, best = candidates[0]
    hd_note = "" if is_hd else "  ⚠ no HD found"
    print(f"  {tag} {title}  score {best_score}{hd_note}")
    print(f"    Name:  {best['name']}")
    print(f"    Added: {fmt_date(best['added'])}  "
          f"Size: {fmt_size(best['size'])}  "
          f"Seeds: {best.get('seeders', '?')}  "
          f"By: {best.get('uploader') or 'unknown'}")
    if len(candidates) > 1:
        alt_score, alt = candidates[1]
        if alt_score >= best_score * 0.85:
            print(f"    Alt (score {alt_score}): {alt['name']}")
    print(f"    Magnet: {best['magnet'][:80]}…")
    return prompt_yes_no(f"Queue {tag}?")


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
    last_ep: str | None = None, show_id: int | None = None,
    episode: str | None = None, season: int | None = None, hint: bool = False,
) -> list[Pick]:
    """Search, score and prompt for one title. `episode` ("S02E01") or
    `season` asks for that episode or whole season of `show`, whenever it
    aired. `hint` suggests a season query when nothing aired recently."""
    suffix = f" {episode}" if episode else f" S{season:02d}" if season is not None else ""
    print(f"{'='*60}")
    print(f"  {show}{suffix}")
    print(f"{'='*60}")

    today = datetime.now().date()
    expected = None
    packs: set[int] = set()
    src = "TVMaze-verified"
    all_eps = None if is_movie else tvmaze_episodes(show, show_id)

    if episode:
        aired = (all_eps or {}).get(episode)
        if aired:
            print(f"  TVMaze: {aired.label}\n")
        else:
            print("  TVMaze: episode not found — searching anyway\n")
            src = "requested episode"
        expected = {episode: aired or Aired(episode, None)}
    elif season is not None:
        packs = {season}
        expected = {k: a for k, a in (all_eps or {}).items()
                    if ep_tuple(k)[0] == season and a.airdate and a.airdate <= today}
        if expected:
            print(f"  TVMaze: season {season}, {len(expected)} episode(s) aired\n")
        else:
            print("  TVMaze: season not found — searching for packs anyway\n")
            src = "requested season"
    elif not is_movie:
        if all_eps is None:
            print("  TVMaze: lookup failed or show not found — using date filter only\n")
        else:
            expected = aired_within(all_eps, DAYS_BACK, today)
            if not expected:
                print(f"  TVMaze: nothing aired in the last {DAYS_BACK} days.")
                past = [(a.airdate, k) for k, a in all_eps.items() if a.airdate and a.airdate <= today]
                if hint and past:
                    when, key = max(past)
                    print(f"  Latest: {all_eps[key].label} — for that season: "
                          f'get_content.py "{show} S{ep_tuple(key)[0]:02d}"')
                print()
                return []
            ordered = sorted(expected, key=_ep_sort_key)
            print(f"  TVMaze: {', '.join(expected[k].label for k in ordered)}")
            last = ep_tuple(last_ep)
            expected = {k: expected[k] for k in ordered if not last or ep_tuple(k) > last}
            if not expected:
                print(f"  Already downloaded through {last_ep}.\n")
                return []
            packs = whole_seasons(expected, all_eps)
            print()

    if expected or packs:
        # One search per season pack and per aired episode, so a busy week of
        # uploads can't push the one we want off the single results page.
        queries = [f"{show} S{s:02d}" for s in sorted(packs)] + [f"{show} {k}" for k in expected or {}]
        found, seen = [], set()
        for q in queries:
            for t in search_torrents(q, sort=3):
                if t["magnet"] not in seen:
                    seen.add(t["magnet"])
                    found.append(t)
    else:
        found = search_torrents(show, sort=99 if is_movie else 3)

    def runtime(t: dict) -> int | None:
        if not expected:
            return None
        season_no = pack_season(t["name"])
        if season_no is not None:
            eps = [a for k, a in expected.items() if ep_tuple(k)[0] == season_no]
            return sum(a.runtime for a in eps) if eps and all(a.runtime for a in eps) else None
        aired = expected.get(episode_key(t["name"]))
        return aired.runtime if aired else None

    matches = title_matches if is_movie else tv_title_matches
    matched = [t for t in found if matches(t["name"], show)]
    scored = [(s, t) for t in matched
              if (s := score_torrent(t, cutoff, is_tv=not is_movie, runtime_min=runtime(t))) is not None]
    scored.sort(key=rank_key, reverse=True)

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
    return _display_tv(show, scored, expected, last_ep=last_ep, src=src, packs=packs)


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
        show, episode, season = (args.query, None, None) if is_movie else parse_query(args.query)
        if episode:
            cutoff, mode_label = 0, "TV, single episode, any date"
        elif season is not None:
            cutoff, mode_label = 0, "TV, whole season, any date"
        elif is_movie:
            cutoff, mode_label = movie_cutoff, "movie, all-time"
        else:
            cutoff, mode_label = tv_cutoff, f"TV, last {DAYS_BACK} days"
        print(f"Searching piratebay.party — {mode_label}\n")
        picks = _search_and_display(show, is_movie=is_movie, cutoff=cutoff,
                                    episode=episode, season=season, hint=True)
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
