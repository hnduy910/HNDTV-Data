import json
from pathlib import Path
import tempfile
import unittest

from backend.update_data import AppendOnlyPipeline, build_manifest, normalise_url, parse_m3u


def block(name, url, *, tvg_id=None, group="Vietnam", country="VN"):
    attrs = [f'group-title="{group}"', f'tvg-country="{country}"']
    if tvg_id:
        attrs.insert(0, f'tvg-id="{tvg_id}"')
    return f'#EXTINF:-1 {" ".join(attrs)},{name}\n{url}\n'


def playlist(*entries):
    return "#EXTM3U\n" + "".join(entries)


class AppendOnlyPipelineTests(unittest.TestCase):
    def run_pipeline(self, old_text, discovery_text, probe_results=None, probe_error_urls=None):
        probe_results = probe_results or {}
        probe_error_urls = {normalise_url(url) for url in (probe_error_urls or [])}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hndtv.m3u").write_text(old_text, encoding="utf-8")
            (root / "channels.json").write_text(
                json.dumps({"schema": 1, "generatedAt": None, "channels": []}),
                encoding="utf-8",
            )
            (root / "sources.json").write_text(
                json.dumps({
                    "schema": 2,
                    "upstreams": [{
                        "id": "fixture-community",
                        "name": "Fixture community source",
                        "enabled": True,
                        "public": True,
                        "productionEligible": True,
                        "permission": "test fixture only",
                    }],
                }),
                encoding="utf-8",
            )

            def fetcher(_source):
                return discovery_text

            def prober(entry, _source):
                if normalise_url(entry.url) in probe_error_urls:
                    raise TimeoutError("fixture timeout")
                return probe_results.get(normalise_url(entry.url), {"live": True, "health": 100})

            report = AppendOnlyPipeline(root, fetcher=fetcher, prober=prober).run()
            result_text = (root / "hndtv.m3u").read_text(encoding="utf-8")
            manifest = json.loads((root / "channels.json").read_text(encoding="utf-8"))
            return report, result_text, manifest

    def test_case_1_existing_alive_is_kept_without_reprobe(self):
        old = playlist(block("VTV1", "https://old.example/vtv1.m3u8", tvg_id="VTV1"))
        report, result, _ = self.run_pipeline(old, old, probe_error_urls=["https://old.example/vtv1.m3u8"])
        self.assertEqual(result, old)
        self.assertEqual(report["urlAdded"], 0)
        self.assertEqual(report["oldUrlRemoved"], 0)

    def test_case_2_existing_dead_is_still_kept(self):
        old = playlist(block("VTV2", "https://old.example/vtv2.m3u8", tvg_id="VTV2"))
        report, result, _ = self.run_pipeline(old, old, probe_results={normalise_url("https://old.example/vtv2.m3u8"): {"live": False}})
        self.assertEqual(result, old)
        self.assertEqual(report["oldUniqueUrlCount"], report["totalUniqueUrlCount"])

    def test_case_3_existing_timeout_is_still_kept(self):
        old = playlist(block("VTV3", "https://old.example/vtv3.m3u8", tvg_id="VTV3"))
        report, result, _ = self.run_pipeline(old, old, probe_error_urls=["https://old.example/vtv3.m3u8"])
        self.assertEqual(result, old)
        self.assertEqual(report["oldUrlRemoved"], 0)

    def test_case_4_new_vietnamese_live_url_is_appended(self):
        old = playlist(block("VTV1", "https://old.example/vtv1.m3u8", tvg_id="VTV1"))
        new_url = "https://new.example/vtv2.m3u8"
        report, result, _ = self.run_pipeline(
            old,
            playlist(block("VTV2", new_url, tvg_id="VTV2")),
        )
        self.assertIn(new_url, result)
        self.assertEqual(report["candidateVietnamese"], 1)
        self.assertEqual(report["newPassed"], 1)
        self.assertEqual(report["urlAdded"], 1)

    def test_case_5_new_vietnamese_dead_url_is_not_appended(self):
        old = playlist(block("VTV1", "https://old.example/vtv1.m3u8", tvg_id="VTV1"))
        new_url = "https://dead.example/vtv2.m3u8"
        report, result, _ = self.run_pipeline(
            old,
            playlist(block("VTV2", new_url, tvg_id="VTV2")),
            probe_results={normalise_url(new_url): {"live": False}},
        )
        self.assertNotIn(new_url, result)
        self.assertEqual(report["urlAdded"], 0)
        self.assertEqual(report["oldUrlRemoved"], 0)

    def test_case_6_new_international_movie_is_appended(self):
        old = playlist(block("VTV1", "https://old.example/vtv1.m3u8", tvg_id="VTV1"))
        movie_url = "https://movie.example/cinema.m3u8"
        report, result, _ = self.run_pipeline(
            old,
            playlist(block("Cinema World", movie_url, group="Movies", country="US")),
        )
        self.assertIn(movie_url, result)
        self.assertEqual(report["candidateInternationalMovies"], 1)

    def test_case_7_new_international_football_is_appended(self):
        old = playlist(block("VTV1", "https://old.example/vtv1.m3u8", tvg_id="VTV1"))
        football_url = "https://sport.example/football.m3u8"
        report, result, _ = self.run_pipeline(
            old,
            playlist(block("World Football", football_url, group="Sports", country="US")),
        )
        self.assertIn(football_url, result)
        self.assertEqual(report["candidateInternationalFootball"], 1)

    def test_case_8_new_unscoped_international_channel_is_ignored(self):
        old = playlist(block("VTV1", "https://old.example/vtv1.m3u8", tvg_id="VTV1"))
        news_url = "https://foreign.example/news.m3u8"
        report, result, _ = self.run_pipeline(
            old,
            playlist(block("World News", news_url, group="News", country="US")),
        )
        self.assertNotIn(news_url, result)
        self.assertEqual(report["filteredOut"], 1)

    def test_case_9_duplicate_existing_url_is_not_added_twice(self):
        url = "https://old.example/vtv1.m3u8"
        old = playlist(block("VTV1", url, tvg_id="VTV1"))
        report, result, _ = self.run_pipeline(old, old)
        self.assertEqual(result, old)
        self.assertEqual(report["duplicateSkipped"], 1)
        self.assertEqual(report["urlAdded"], 0)

    def test_case_10_same_channel_keeps_a_different_new_source(self):
        first = "https://old.example/vtv1.m3u8"
        second = "https://new.example/vtv1.m3u8"
        old = playlist(block("VTV1", first, tvg_id="VTV1"))
        report, result, manifest = self.run_pipeline(
            old,
            playlist(block("VTV 1 HD", second, tvg_id="VTV1")),
        )
        self.assertIn(second, result)
        self.assertEqual(report["urlAdded"], 1)
        vtv1 = next(channel for channel in manifest["channels"] if channel["name"] == "VTV1")
        self.assertEqual(len(vtv1["streams"]), 2)

    def test_case_11_probe_failure_never_removes_old_urls(self):
        old_url = "https://old.example/vtv1.m3u8"
        new_url = "https://new.example/vtv2.m3u8"
        old = playlist(block("VTV1", old_url, tvg_id="VTV1"))
        report, result, _ = self.run_pipeline(old, playlist(block("VTV2", new_url, tvg_id="VTV2")), probe_error_urls=[new_url])
        self.assertEqual(result, old)
        self.assertEqual(report["probeFailed"], 1)
        self.assertEqual(report["oldUrlRemoved"], 0)

    def test_case_12_zero_candidates_preserves_old_bytes(self):
        old = playlist(
            block("VTV1", "https://old.example/vtv1.m3u8", tvg_id="VTV1"),
            block("VTV2", "https://old.example/vtv2.m3u8", tvg_id="VTV2"),
        )
        report, result, _ = self.run_pipeline(old, "#EXTM3U\n")
        self.assertEqual(result, old)
        self.assertEqual(report["candidateCount"], 0)
        self.assertEqual(report["oldUniqueUrlCount"], report["totalUniqueUrlCount"])
        self.assertEqual(report["oldUrlRemoved"], 0)

    def test_manifest_uses_ios_group_order_natural_numbers_and_one_channel_tile(self):
        entries = playlist(
            block("ON Music", "https://example.test/on.m3u8", tvg_id="ON-MUSIC", group="VTVcab", country="VN"),
            block("VTV10", "https://example.test/vtv10.m3u8", tvg_id="VTV10"),
            block("VTV9", "https://example.test/vtv9.m3u8", tvg_id="VTV9"),
            block("VTV1", "https://example.test/vtv1.m3u8", tvg_id="VTV1"),
            block("VTV 1 HD", "https://example.test/vtv1-hd.m3u8", tvg_id="VTV1"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hndtv.m3u").write_text(entries, encoding="utf-8")
            manifest = {"schema": 1, "channels": []}
            result = build_manifest(parse_m3u(entries), manifest)
        names = [channel["name"] for channel in result["channels"]]
        self.assertEqual(names[:4], ["VTV1", "VTV9", "VTV10", "ON Music"])
        vtv1 = next(channel for channel in result["channels"] if channel["name"] == "VTV1")
        self.assertEqual(len(vtv1["streams"]), 2)


if __name__ == "__main__":
    unittest.main()
