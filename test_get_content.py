"""Offline tests for get_content.py — run with: python3 -m unittest"""

import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import get_content as gc

GB = 1024 ** 3

# Two rows in piratebay.party's column layout (see CLAUDE.md), plus a header row.
FIXTURE = """
<table id="searchResult">
<tr><th>Type</th><th>Name</th></tr>
<tr>
<td class="vertTh"><a href="/browse/208">TV</a></td>
<td><a href="/torrent/1/" title="Details for Grey&#039;s.Anatomy.S21E03.1080p.WEB.H265-GRP">x</a></td>
<td>Today&nbsp;09:15</td>
<td><nobr><a href="magnet:?xt=urn:btih:ABC&amp;dn=Greys&amp;tr=udp%3A%2F%2Ftracker">m</a></nobr></td>
<td align="right">1.50&nbsp;GiB</td>
<td align="right">123</td>
<td align="right">4</td>
<td><a href="/user/EZTV/" title="Browse EZTV">EZTV</a></td>
</tr>
<tr>
<td class="vertTh"><a href="/browse/201">Movies</a></td>
<td><a href="/torrent/2/" title="Details for Old.Movie.1999.720p">x</a></td>
<td>03-14&nbsp;2019</td>
<td><nobr><a href="magnet:?xt=urn:btih:DEF">m</a></nobr></td>
<td align="right">700.00&nbsp;MiB</td>
<td align="right">5</td>
<td align="right">0</td>
<td><a href="/user/someone/" title="Browse someone">someone</a></td>
</tr>
</table>
"""


def torrent(name, *, added=10**10, seeders=0, size=0, uploader=""):
    return {"name": name, "magnet": f"magnet:?xt={name}", "added": added,
            "seeders": seeders, "size": size, "uploader": uploader}


class ParsingTests(unittest.TestCase):
    def test_rows_are_unescaped(self):
        rows = gc._parse_rows(FIXTURE)
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(first["name"], "Grey's.Anatomy.S21E03.1080p.WEB.H265-GRP")
        self.assertEqual(first["magnet"], "magnet:?xt=urn:btih:ABC&dn=Greys&tr=udp%3A%2F%2Ftracker")
        self.assertEqual(first["size"], int(1.5 * GB))
        self.assertEqual((first["seeders"], first["leechers"]), (123, 4))
        self.assertEqual(first["uploader"], "EZTV")
        self.assertGreater(first["added"], 0)
        self.assertEqual(rows[1]["added"], int(datetime(2019, 3, 14).timestamp()))

    def test_date_formats(self):
        now = datetime(2026, 10, 1, 12, 0)
        cases = {
            "5&nbsp;mins&nbsp;ago": now - timedelta(minutes=5),
            "Today 09:15":          datetime(2026, 10, 1, 9, 15),
            "Y-day\xa023:59":       datetime(2026, 9, 30, 23, 59),
            "09-28 10:00":          datetime(2026, 9, 28, 10, 0),
            "12-25 10:00":          datetime(2025, 12, 25, 10, 0),  # future → last year
            "03-14 2019":           datetime(2019, 3, 14),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(gc._parse_date(raw, now), int(expected.timestamp()))
        self.assertEqual(gc._parse_date("garbage", now), 0)

    def test_clean_query_strips_curly_apostrophes(self):
        self.assertEqual(gc.clean_query("Grey’s Anatomy"), "Greys Anatomy")
        self.assertEqual(gc.clean_query("Bob's Burgers"), "Bobs Burgers")
        self.assertEqual(gc.clean_query("Dexter: Resurrection"), "Dexter Resurrection")


class EpisodeKeyTests(unittest.TestCase):
    def test_three_digit_episodes(self):
        self.assertEqual(gc.episode_key("Show.S01E105.1080p"), "S01E105")
        self.assertGreater(gc.ep_tuple("S01E100"), gc.ep_tuple("S01E99"))
        self.assertEqual(gc.episode_key("Show.2024.1080p"), "UNKNOWN")

    def test_grouping_sorts_numerically(self):
        scored = [(1, torrent("S.S01E100.1080p")), (1, torrent("S.S01E99.1080p"))]
        self.assertEqual(list(gc.group_by_episode(scored, drop_stale=False)), ["S01E99", "S01E100"])

    def test_stale_filter_is_optional(self):
        scored = [(1, torrent(f"S.S01E{n:02d}.1080p")) for n in range(1, 11)]
        self.assertEqual(len(gc.group_by_episode(scored, drop_stale=False)), 10)
        kept = gc.group_by_episode(scored)
        self.assertEqual(list(kept), [f"S01E{n:02d}" for n in range(5, 11)])


class ScoringTests(unittest.TestCase):
    def score(self, name, **kw):
        is_tv = kw.pop("is_tv", True)
        runtime_min = kw.pop("runtime_min", None)
        return gc.score_torrent(torrent(name, **kw), 0, is_tv=is_tv, runtime_min=runtime_min)

    def test_tokens_respect_word_starts(self):
        self.assertEqual(self.score("Show.EAC3"), 15)      # not also "ac3"
        self.assertEqual(self.score("Show.DDP5.1"), 15)    # trailing digits fine
        self.assertEqual(self.score("Show.DTS-HD.MA.5.1"), 25)
        self.assertEqual(self.score("Show.H.265"), 70)
        self.assertEqual(self.score("Havc.24k"), 0)

    def test_movie_size_bonus_plateaus(self):
        at = lambda gb: self.score("Movie.2020", size=int(gb * GB), seeders=1, is_tv=False)
        base = at(1)
        self.assertEqual(at(4) - base, 20)
        self.assertEqual(at(6) - base, 40)
        self.assertEqual(at(8) - base, 40)
        self.assertIsNone(at(11))

    def test_seeder_bonus(self):
        seeds = lambda n: self.score("Show", seeders=n)
        self.assertEqual(seeds(0), 0)
        self.assertEqual(seeds(4), 25)
        self.assertEqual(seeds(29), 50)    # capped
        self.assertEqual(seeds(5000), 50)

    def test_unseeded_movies_dropped_tv_kept(self):
        self.assertIsNone(self.score("Movie.2020", seeders=0, is_tv=False))
        self.assertEqual(self.score("Show.S01E01", seeders=0), 0)

    def test_movie_mode_rejects_episodes(self):
        self.assertIsNone(self.score("Show.S01E01.1080p", is_tv=False))

    def test_source(self):
        self.assertEqual(self.score("Show 1080p WEB h264-ETHEL") - self.score("Show 1080p h264"), 30)
        self.assertEqual(self.score("Show.1080p.WEB-DL.H264"), self.score("Show.1080p.WEB.H264"))
        self.assertEqual(self.score("Show.WEBRip"), 15)
        self.assertEqual(self.score("Show.HDTV"), 5)
        self.assertEqual(self.score("Webcam.Show"), 0)

    def test_overcompressed_penalty(self):
        mb = 1024 ** 2
        at = lambda name, size_mb: self.score(name, size=int(size_mb * mb), runtime_min=30)
        # 266 MB of 1080p HEVC over a 30-min slot ≈ 1.2 Mbit/s: squeezed
        self.assertEqual(at("Show 1080p HEVC x265", 266), 70 + 60 - gc.OVERCOMPRESSED_PENALTY)
        self.assertEqual(at("Show 1080p x265", 533), 70 + 60)
        # H.264 needs more bits than HEVC for the same quality
        self.assertEqual(at("Show 1080p h264", 533), 60 + 60 - gc.OVERCOMPRESSED_PENALTY)
        self.assertEqual(at("Show 1080p h264", 995), 60 + 60)
        # No runtime (movies, date-filter fallback) → no check
        self.assertEqual(self.score("Show 1080p HEVC", size=266 * mb), 130)

    def test_ties_broken_by_seeders(self):
        few, many = (1, torrent("a", seeders=160)), (1, torrent("b", seeders=1037))
        self.assertEqual(sorted([few, many], key=gc.rank_key, reverse=True)[0], many)


class TVMazeTests(unittest.TestCase):
    def run_with(self, episodes):
        with mock.patch.object(gc, "_tvmaze_get", return_value=episodes):
            return gc.tvmaze_aired_this_week("Show", 6, show_id=1)

    def test_found_but_nothing_aired_is_empty_not_none(self):
        self.assertEqual(self.run_with([{"season": 1, "number": 1, "airdate": "2000-01-01"}]), {})

    def test_failure_is_none(self):
        self.assertIsNone(self.run_with(None))

    def test_episode_lookup_ignores_date(self):
        eps = [{"season": 2, "number": 1, "airdate": "2002-10-03", "name": "Old", "runtime": 30}]
        with mock.patch.object(gc, "_tvmaze_get", return_value=eps):
            aired = gc.tvmaze_episode("Show", "S02E01", show_id=1)
        self.assertEqual((aired.label, aired.runtime), ("S02E01 · Old (2002-10-03)", 30))

    def test_year_picks_series(self):
        hits = [{"show": {"id": 532, "premiered": "2001-10-02"}},
                {"show": {"id": 84836, "premiered": "2026-02-25"}}]
        with mock.patch.object(gc, "_tvmaze_get", return_value=hits) as get:
            self.assertEqual(gc.tvmaze_find_show("Scrubs 2026"), 84836)
            self.assertIn("q=Scrubs", get.call_args[0][0])
            self.assertNotIn("2026", get.call_args[0][0])
            self.assertEqual(gc.tvmaze_find_show("Scrubs (2001)"), 532)
            self.assertEqual(gc.tvmaze_find_show("Scrubs"), 532)  # top hit

    def test_specials_are_skipped(self):
        today = datetime.now().strftime("%Y-%m-%d")
        aired = self.run_with([
            {"season": 1, "number": None, "airdate": today, "name": "Special"},
            {"season": 1, "number": 2, "airdate": today, "name": "Two"},
        ])
        self.assertEqual(list(aired), ["S01E02"])


class EpisodeQueryTests(unittest.TestCase):
    def test_split(self):
        self.assertEqual(gc.split_episode_query("Scrubs 2026 S02E01"), ("Scrubs 2026", "S02E01"))
        self.assertEqual(gc.split_episode_query("Scrubs s2e1"), ("Scrubs", "S02E01"))
        self.assertEqual(gc.split_episode_query("Severance"), ("Severance", None))


class DisplayTvTests(unittest.TestCase):
    def picks(self, scored, expected, answers, last_ep=None):
        with mock.patch.object(gc, "prompt_yes_no", side_effect=answers), \
             mock.patch("builtins.print"):
            return gc._display_tv("Show", scored, expected, last_ep=last_ep)

    def test_whole_season_release_keeps_early_episodes(self):
        expected = {f"S01E{n:02d}": gc.Aired(f"S01E{n:02d} · T", 30) for n in range(1, 11)}
        scored = [(1, torrent(f"Show.S01E{n:02d}.1080p")) for n in range(1, 11)]
        picks = self.picks(scored, expected, [True] * 10)
        self.assertEqual(len(picks), 10)
        self.assertTrue(all(p.advances for p in picks))

    def test_declined_episode_blocks_history(self):
        scored = [(1, torrent("Show.S01E05.1080p")), (1, torrent("Show.S01E06.1080p"))]
        picks = self.picks(scored, None, [False, True])
        self.assertEqual([(p.ep_key, p.advances) for p in picks], [("S01E06", False)])

    def test_missing_earlier_episode_blocks_history(self):
        expected = {"S01E05": gc.Aired("S01E05 · A", 30), "S01E06": gc.Aired("S01E06 · B", 30)}
        picks = self.picks([(1, torrent("Show.S01E06.1080p"))], expected, [True])
        self.assertEqual([(p.ep_key, p.advances) for p in picks], [("S01E06", False)])


class ContentListTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / ".content_list.json"
        patches = [
            mock.patch.object(gc, "CONTENT_LIST", self.path),
            mock.patch.object(gc, "sync_content_list_with_peer"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.dir.cleanup)

    def test_save_is_atomic_and_keeps_mode(self):
        self.path.write_text("{}")
        os.chmod(self.path, 0o644)
        gc.save_content_list({"shows": []})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
        self.assertEqual(os.listdir(self.dir.name), [self.path.name])

    def test_record_progress(self):
        self.path.write_text(json.dumps({"shows": ["A", "B", "C"],
                                         "last_downloaded": {"A": "S01E09"}}))
        picks = [
            gc.Pick("m1", "A", "S01E10", True),
            gc.Pick("m2", "A", "S01E11", True),   # failed to send
            gc.Pick("m3", "A", "S01E12", True),   # after a failure → not recorded
            gc.Pick("m4", "B", "S02E01", False),
            gc.Pick("m5", "C", "S01E100", True),
            gc.Pick("m6"),                         # movie
        ]
        gc.record_progress(picks, [True, False, True, True, True, True])
        ld = json.loads(self.path.read_text())["last_downloaded"]
        self.assertEqual(ld, {"A": "S01E10", "C": "S01E100"})


if __name__ == "__main__":
    unittest.main()
