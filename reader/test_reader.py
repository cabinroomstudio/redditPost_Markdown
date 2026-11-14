"""Run: venv\\Scripts\\python -m unittest reader.test_reader"""
import json
import tempfile
import unittest
from pathlib import Path

from reader.app import Api
from reader.notes import Library, State, parse_front_matter, render_html

NOTE = """---
title: "A \\"quoted\\" title"
url: https://www.reddit.com/r/x/comments/abc/
post_id: abc
subreddit: ClaudeAI
version: 2
post_score: 12
upvote_ratio: 0.9
tags: [reddit, ClaudeAI]
---

# A "quoted" title

[r/ClaudeAI](https://www.reddit.com/r/x/comments/abc/) · u/me · 12 pts

Body text with <script>alert(1)</script> and a link <https://example.com>.

## Comments (1)

### u/someone — 3 pts

hello
"""


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "a-quoted-title--abc.md").write_text(NOTE, encoding="utf-8")
        (self.dir / "my-doc.md").write_text("# Hand written\n\nsome text\n", encoding="utf-8")
        v = self.dir / "_versions"
        v.mkdir()
        (v / "a-quoted-title--abc.v1.md").write_text(NOTE.replace("hello", "old"), encoding="utf-8")
        (v / "other--zzz.v1.md").write_text("x", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_front_matter(self):
        meta, body = parse_front_matter(NOTE)
        self.assertEqual(meta["title"], 'A "quoted" title')
        self.assertEqual(meta["post_score"], 12)
        self.assertEqual(meta["upvote_ratio"], 0.9)
        self.assertEqual(meta["tags"], ["reddit", "ClaudeAI"])
        self.assertTrue(body.lstrip().startswith("# A"))

    def test_library_includes_hand_written_docs(self):
        notes, skipped = Library(self.dir).notes()
        self.assertEqual(skipped, [])
        titles = {n.summary()["title"] for n in notes}
        self.assertEqual(titles, {'A "quoted" title', "Hand written"})

    def test_versions_only_for_this_note(self):
        lib = Library(self.dir)
        note = lib.get("abc")
        self.assertEqual([v["version"] for v in lib.versions(note)], [1])
        self.assertIn("old", lib.version_body(note, "a-quoted-title--abc.v1.md"))
        self.assertIsNone(lib.version_body(note, "other--zzz.v1.md"))
        self.assertIsNone(lib.version_body(note, "../a-quoted-title--abc.md"))

    def test_raw_html_is_escaped(self):
        html = render_html(NOTE)
        self.assertNotIn("<script", html)
        self.assertIn('href="https://example.com"', html)

    def test_search_matches_comments(self):
        lib = Library(self.dir)
        self.assertEqual(lib.search("HELLO someone"), ["abc"])
        self.assertEqual(lib.search("nothing-here"), [])

    def test_state_round_trip_and_updated_flag(self):
        api = Api(self.dir)
        api.mark_read("abc", True)
        api.update_note("abc", {"starred": True, "tags": ["x"], "bogus": 1})
        api.update_settings({"theme": "dark", "nope": 1})
        data = json.loads((self.dir / ".reader-state.json").read_text(encoding="utf-8"))
        self.assertTrue(data["notes"]["abc"]["starred"])
        self.assertNotIn("bogus", data["notes"]["abc"])
        self.assertEqual(data["settings"]["theme"], "dark")
        self.assertNotIn("nope", data["settings"])
        item = next(n for n in Api(self.dir).library()["notes"] if n["id"] == "abc")
        self.assertFalse(item["updated"])
        api.update_note("abc", {"read_at": 1.0})  # read long before the file changed
        item = next(n for n in Api(self.dir).library()["notes"] if n["id"] == "abc")
        self.assertTrue(item["updated"])

    def test_broken_state_file_is_kept_not_overwritten(self):
        (self.dir / ".reader-state.json").write_text("{not json", encoding="utf-8")
        State.load(self.dir).save()
        self.assertEqual(len(list(self.dir.glob(".reader-state.broken-*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
