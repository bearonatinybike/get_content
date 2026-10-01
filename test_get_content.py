"""Offline tests for get_content.py — run with: python3 -m unittest"""

import json
import os
import stat
import tempfile
import unittest
from datetime import date, datetime, timedelta
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
        audio = lambda n: (self.score(f"Movie.{n}", seeders=1, is_tv=False)
                           - self.score("Movie", seeders=1, is_tv=False))
        self.assertEqual(audio("EAC3"), 15)      # not also "ac3"
        self.assertEqual(audio("DDP5.1"), 15)    # trailing digits fine
        self.assertEqual(audio("DTS-HD.MA.5.1"), 25)
        self.assertEqual(self.score("Show.H.265"), 70)
        self.assertEqual(self.score("Havc.24k"), 0)

    def test_tv_ignores_audio(self):
        self.assertEqual(self.score("Show.S01E01.1080p.WEB.DDP5.1.Atmos.H264"),
                         self.score("Show.S01E01.1080p.WEB.H264"))

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

    def test_movie_size_scales_for_4k(self):
        at = lambda name, gb: self.score(name, size=int(gb * GB), seeders=1, is_tv=False)
        base = at("Movie 2160p", 5)
        self.assertEqual(at("Movie 2160p", 12) - base, 20)
        self.assertEqual(at("Movie 2160p", 18.3) - base, 40)
        self.assertIsNone(at("Movie 2160p", 31))
        self.assertIsNone(at("Movie 1080p", 11))

    def test_title_whole_words(self):
        self.assertFalse(gc.title_matches("Toy Story 4 2019 1080p BluRay HEVC x265 5.1-SUBS", "Toy Story 5"))
        self.assertTrue(gc.title_matches("Toy.Story.5.2026.1080p.WEB.h264-ETHEL", "Toy Story 5"))
        self.assertTrue(gc.title_matches("Spider.Man.2002.1080p", "Spider-Man"))
        self.assertTrue(gc.title_matches("Greys.Anatomy.S21E03", "Grey\u2019s Anatomy"))
        self.assertTrue(gc.title_matches("Bob's Burgers S16E01", "Bob's Burgers"))
        self.assertFalse(gc.title_matches("Dunes.2020.1080p", "Dune"))

    def test_movie_mode_rejects_episodes(self):
        self.assertIsNone(self.score("Show.S01E01.1080p", is_tv=False))

    def test_source(self):
        self.assertEqual(self.score("Show 1080p WEB h264-ETHEL") - self.score("Show 1080p h264"), 30)
        self.assertEqual(self.score("Show.1080p.WEB-DL.H264"), self.score("Show.1080p.WEB.H264"))
        self.assertEqual(self.score("Show.WEBRip"), 10)
        self.assertEqual(self.score("Show.HDTV"), 5)
        self.assertEqual(self.score("Webcam.Show"), 0)
        self.assertEqual(self.score("Movie.1080p.DCPRIP.x264", is_tv=False, seeders=1)
                         - self.score("Movie.1080p.x264", is_tv=False, seeders=1), 5)

    def test_web_reencode_scores_as_rip(self):
        # "WEB-DL ... x265" = the group's encode of the download
        self.assertEqual(self.score("Show 1080P ATVP WEB-DL DDP5.1 Atmos. X265 POOTLED")
                         - self.score("Show 1080P X265"), gc.WEB_ENCODE_SCORE)
        # "H265"/"HEVC" without an encoder tag can be the untouched stream
        self.assertEqual(self.score("Show.2160p.AMZN.WEB-DL.DV.H265")
                         - self.score("Show.2160p.DV.H265"), 30)

    def test_cinema_recordings_dropped(self):
        for name in ("Movie.2026.1080p.TELESYNC.x264", "Movie 2026 1080p CAM x264",
                     "Movie.2025.1080p.hdts.h264", "Movie (2026) HDTS x265", "Movie.TS.720p"):
            with self.subTest(name=name):
                self.assertIsNone(self.score(name, seeders=10, is_tv=False))
        self.assertIsNotNone(self.score("Tsunami.2004.1080p", seeders=10, is_tv=False))

    def test_foreign_penalty(self):
        base = self.score("Show.S01E01.1080p.WEB-DL.H265-TBK")
        for tag in ("ENG.ITA", "MULTi", "Dual", "Hindi.Dubbed", "FRENCH", "NORDiC", "VFF"):
            with self.subTest(tag=tag):
                self.assertEqual(self.score(f"Show.S01E01.1080p.WEB-DL.{tag}.H265-TBK"),
                                 base - gc.FOREIGN_PENALTY)
        self.assertEqual(self.score("Show.S01E01.1080p.WEB-DL.ENG.H265"), base)

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
    def episodes(self, raw):
        with mock.patch.object(gc, "_tvmaze_get", return_value=raw):
            return gc.tvmaze_episodes("Show", show_id=1)

    def test_failure_is_none(self):
        self.assertIsNone(self.episodes(None))

    def test_found_but_nothing_aired_is_empty_not_none(self):
        eps = self.episodes([{"season": 1, "number": 1, "airdate": "2000-01-01"}])
        self.assertEqual(gc.aired_within(eps, 6), {})

    def test_window_excludes_future(self):
        today = date(2026, 10, 1)
        eps = self.episodes([{"season": 1, "number": n, "airdate": d} for n, d in
                             [(1, "2026-09-20"), (2, "2026-09-28"), (3, "2026-10-05")]])
        self.assertEqual(list(gc.aired_within(eps, 6, today)), ["S01E02"])

    def test_episode_fields(self):
        eps = self.episodes([{"season": 2, "number": 1, "airdate": "2002-10-03", "name": "Old", "runtime": 30}])
        aired = eps["S02E01"]
        self.assertEqual((aired.label, aired.runtime, aired.airdate),
                         ("S02E01 · Old (2002-10-03)", 30, date(2002, 10, 3)))

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
        eps = self.episodes([
            {"season": 1, "number": None, "airdate": "2026-09-16", "name": "Special"},
            {"season": 1, "number": 2, "airdate": "2026-09-16", "name": "Two"},
        ])
        self.assertEqual(list(eps), ["S01E02"])

    def test_whole_seasons(self):
        a = lambda: gc.Aired("x", 50)
        everything = {"S01E01": a(), "S01E02": a(), "S02E01": a(), "S02E02": a(), "S03E01": a()}
        expected = {"S01E01": a(), "S01E02": a(), "S02E02": a(), "S03E01": a()}
        # S02 only partly in the window; S03 has a single episode
        self.assertEqual(gc.whole_seasons(expected, everything), {1})


class QueryTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(gc.parse_query("Scrubs 2026 S02E01"), ("Scrubs 2026", "S02E01", None))
        self.assertEqual(gc.parse_query("Scrubs s2e1"), ("Scrubs", "S02E01", None))
        self.assertEqual(gc.parse_query("Neagley S01"), ("Neagley", None, 1))
        self.assertEqual(gc.parse_query("Neagley Season 2"), ("Neagley", None, 2))
        self.assertEqual(gc.parse_query("Severance"), ("Severance", None, None))

    def test_pack_season(self):
        self.assertEqual(gc.pack_season("Neagley.S01.1080p.AMZN.WEB-DL.DDP5.1.Atmos.H264-FLUX"), 1)
        self.assertEqual(gc.pack_season("Neagley 2026 Season 1 Complete 1080p WEB x264 [i_c]"), 1)
        self.assertEqual(gc.pack_season("Neagley (2026) S01 (1080p AMZN WEB-DL x265 10bit EAC3 Atmos 5.1 Ghost) [QxR]"), 1)
        self.assertIsNone(gc.pack_season("Neagley S01E04 2160P AMZN WEB-DL DD+ 5.1 Atmos DV HDR10+ H.265"))
        self.assertIsNone(gc.pack_season("Show S01-S03 1080p"))
        self.assertIsNone(gc.pack_season("Dune Part Two 2024 1080p"))


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

    def season(self, answers):
        """Season 1 dropped at once: one pack, two episodes, and a stray S02 pack."""
        expected = {f"S01E{n:02d}": gc.Aired(f"S01E{n:02d} · T", 50) for n in (1, 2)}
        scored = [(300, torrent("Show.S01.2160p.WEB-DL")),
                  (200, torrent("Show.S01E01.1080p")), (200, torrent("Show.S01E02.1080p")),
                  (999, torrent("Show.S02.2160p"))]
        prompt = mock.Mock(side_effect=answers)
        with mock.patch.object(gc, "prompt_yes_no", prompt), mock.patch("builtins.print"):
            picks = gc._display_tv("Show", scored, expected, packs={1})
        return picks, [c.args[0] for c in prompt.call_args_list]

    def test_season_pack_taken_skips_episodes(self):
        picks, asked = self.season([True])
        self.assertEqual(asked, ["Queue [S01 pack]?"])
        self.assertEqual([(p.magnet, p.ep_key, p.advances) for p in picks],
                         [("magnet:?xt=Show.S01.2160p.WEB-DL", "S01E02", True)])

    def test_season_pack_declined_offers_episodes(self):
        picks, asked = self.season([False, True, True])
        self.assertEqual(asked, ["Queue [S01 pack]?", "Queue [S01E01]?", "Queue [S01E02]?"])
        self.assertEqual([p.ep_key for p in picks], ["S01E01", "S01E02"])


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
