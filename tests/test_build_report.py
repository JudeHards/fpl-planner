"""Check that reports combine matching inputs and display CSV text safely."""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_report


class RenderedHTML(HTMLParser):
    """Inspect actual HTML elements, rather than escaped source substrings."""

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.text = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


class InputValidationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.team_path = Path(directory.name) / "current-team.md"
        self.team_path.write_text("# Current team\n\nPlayer Example\n", encoding="utf-8")
        self.metadata = {
            "season": "2026-2027",
            "gameweek": 6,
            "team_sha256": hashlib.sha256(self.team_path.read_bytes()).hexdigest(),
            "data_revision": "a" * 40,
        }

    def test_matching_metadata_is_accepted(self):
        build_report.validate_inputs(dict(self.metadata), dict(self.metadata), self.team_path)

    def test_season_or_gameweek_mismatch_is_rejected(self):
        for key, wrong_value in (("season", "2025-2026"), ("gameweek", 7)):
            with self.subTest(key=key):
                transfers = {**self.metadata, key: wrong_value}
                with self.assertRaisesRegex(ValueError, key):
                    build_report.validate_inputs(self.metadata, transfers, self.team_path)

    def test_stale_or_missing_team_hash_in_either_output_is_rejected(self):
        for label in ("Simulation", "Transfers"):
            for missing in (False, True):
                with self.subTest(output=label, missing=missing):
                    simulation, transfers = dict(self.metadata), dict(self.metadata)
                    invalid = simulation if label == "Simulation" else transfers
                    if missing:
                        invalid.pop("team_sha256")
                    else:
                        invalid["team_sha256"] = "b" * 64
                    with self.assertRaisesRegex(ValueError, label + " JSON does not match"):
                        build_report.validate_inputs(simulation, transfers, self.team_path)

    def test_different_training_windows_are_rejected(self):
        for key, first, second in (("training_gameweeks", [1, 2, 3, 4, 5], [3, 4, 5]),
                                    ("training_matches", 50, 30)):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                build_report.validate_inputs({**self.metadata, key: first},
                                             {**self.metadata, key: second}, self.team_path)

    def test_changing_team_file_invalidates_matching_saved_outputs(self):
        self.team_path.write_text("# Current team\n\nReplacement Player\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "current team file"):
            build_report.validate_inputs(self.metadata, self.metadata, self.team_path)

    def test_different_data_revisions_are_rejected(self):
        transfers = {**self.metadata, "data_revision": "b" * 40}
        with self.assertRaisesRegex(ValueError, "different data revisions"):
            build_report.validate_inputs(self.metadata, transfers, self.team_path)

    def test_matching_saved_outputs_must_match_checked_out_data_revision(self):
        # Fake Git's answer: this test must not depend on the real data checkout.
        data_path = self.team_path.parent / "data" / "2026-2027"
        for revision, should_accept in (("a" * 40, True), ("c" * 40, False)):
            with self.subTest(revision=revision), patch(
                "build_report.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, revision + "\n", ""),
            ):
                if should_accept:
                    build_report.validate_inputs(self.metadata, self.metadata, self.team_path, data_path)
                else:
                    with self.assertRaisesRegex(ValueError, "checked-out data revision"):
                        build_report.validate_inputs(self.metadata, self.metadata, self.team_path, data_path)


class HTMLRenderingTests(unittest.TestCase):
    def test_csv_names_and_news_are_visible_text_not_active_html(self):
        name = '<img src=x onerror="alert(1)"> & O\'Brien'
        news = '<script>alert("injury")</script> <b>Fitness & form</b>'
        document = {
            "title": "Report <svg onload=alert(1)>",
            "blocks": [
                build_report.table(["Player", "News"], [[name, news]]),
                build_report.text_block(name + ": " + news),
                {"kind": "list", "items": [news]},
            ],
        }
        rendered = build_report.render_html(document)
        parsed = RenderedHTML(rendered)
        tags = {tag for tag, _ in parsed.elements}
        self.assertTrue({"table", "td", "p", "li"}.issubset(tags))
        self.assertTrue({"script", "img", "svg", "b"}.isdisjoint(tags))
        self.assertIn(name, "".join(parsed.text))
        self.assertIn(news, "".join(parsed.text))
        self.assertIn("&lt;script&gt;", rendered)
        for _, attributes in parsed.elements:
            self.assertFalse(any(key.startswith("on") for key in attributes))

    def test_report_needs_no_external_fonts_stylesheets_or_scripts(self):
        rendered = build_report.render_html({
            "title": "Offline report",
            "blocks": [build_report.text_block(
                "Source: [upstream data](https://github.com/olbauday/FPL-Core-Insights)"
            )],
        })
        parsed = RenderedHTML(rendered)
        self.assertIn("style", {tag for tag, _ in parsed.elements})
        for tag, attributes in parsed.elements:
            self.assertNotIn(tag, {"script", "link", "iframe", "object", "embed"})
            self.assertNotIn("src", attributes)
        self.assertNotRegex(rendered, r"(?i)@import|@font-face|url\s*\(")
        # Ordinary source links are allowed; they do not load a rendering dependency.
        self.assertTrue(any(tag == "a" and attrs.get("href", "").startswith("https://")
                            for tag, attrs in parsed.elements))


if __name__ == "__main__":
    unittest.main()
