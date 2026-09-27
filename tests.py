"""Fixture-driven checks. Run: python tests.py

Covers the parts that are expensive or slow to confirm against the live site:
`more` expansion and splicing, the filter rules, and the version-promotion gates
(which would otherwise take a week of real threads to observe).
"""
from __future__ import annotations

import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from config import Config
from fetch import fetch_thread
from filters import filter_thread, kept_comments
from render import render, slugify
from store import add_version, connect, iso, latest_version, now_utc
from versioning import Metrics, decide, measure

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL  {label} {detail}")


# ---------------- fixtures ----------------

def comment(cid, body, score, parent="t3_test01", author=None, replies=None, **extra):
    data = {
        "id": cid, "author": author or f"user_{cid}", "body": body, "score": score,
        "created_utc": 1_757_000_000, "parent_id": parent, **extra,
    }
    if replies:
        data["replies"] = {"kind": "Listing", "data": {"children": replies}}
    return {"kind": "t1", "data": data}


def more(parent, children):
    return {"kind": "more", "data": {"parent_id": parent, "children": children,
                                     "count": len(children), "id": children[0] if children else "_"}}


def post_listing(score=100, num_comments=12):
    return {"kind": "Listing", "data": {"children": [{"kind": "t3", "data": {
        "id": "test01", "title": "How I actually use AI tools daily",
        "author": "op_user", "subreddit": "ChatGPTPro",
        "selftext": "Here is my workflow.\n\nStep one.", "permalink": "/r/ChatGPTPro/comments/test01/x/",
        "url": "https://www.reddit.com/r/ChatGPTPro/comments/test01/x/",
        "score": score, "upvote_ratio": 0.97, "num_comments": num_comments,
        "created_utc": 1_756_900_000, "is_self": True, "link_flair_text": "Discussion",
    }}]}}


class FakeHttp:
    """Stands in for RedditHttp so expansion logic can be tested offline."""

    def __init__(self, comments_children, more_things=None):
        self.comments_children = comments_children
        self.more_things = more_things or []
        self.post_calls = 0

    def api_get(self, path, params=None):
        return [post_listing(), {"kind": "Listing", "data": {"children": self.comments_children}}]

    def api_post(self, path, data):
        self.post_calls += 1
        return {"json": {"errors": [], "data": {"things": self.more_things}}}

    def final_url(self, url):
        return url


# ---------------- tests ----------------

def test_expansion(cfg: Config) -> None:
    print("\nmore-expansion and splicing")
    children = [
        comment("c1", "A long and genuinely useful top level answer here.", 120,
                replies=[comment("c1r1", "A reply that adds real detail to the answer.", 30,
                                 parent="t1_c1")]),
        comment("c2", "Another substantive top level comment with content.", 45),
        more("t3_test01", ["m1", "m2"]),
    ]
    more_things = [
        {"kind": "t1", "data": {"id": "m1", "author": "late_user",
                                "body": "A late comment that arrived via morechildren.",
                                "score": 15, "created_utc": 1_757_100_000,
                                "parent_id": "t3_test01"}},
        # Deliberately out of order: child before its parent.
        {"kind": "t1", "data": {"id": "m2b", "author": "deep_user",
                                "body": "A nested reply delivered before its parent.",
                                "score": 8, "created_utc": 1_757_100_500,
                                "parent_id": "t1_m2"}},
        {"kind": "t1", "data": {"id": "m2", "author": "other_user",
                                "body": "Parent delivered after its own child.",
                                "score": 12, "created_utc": 1_757_100_200,
                                "parent_id": "t1_c2"}},
    ]
    thread = fetch_thread("test01", FakeHttp(children, more_things), cfg.reddit, log=lambda *a: None)
    ids = {c.id for c in thread.all_comments()}
    check("all placeholder comments spliced in", ids == {"c1", "c1r1", "c2", "m1", "m2", "m2b"},
          f"got {sorted(ids)}")

    by_id = {c.id: c for c in thread.all_comments()}
    check("out-of-order child attached to its parent", by_id["m2b"].depth == 2,
          f"depth={by_id['m2b'].depth}")
    check("morechildren root comment is top level", by_id["m1"].depth == 0)
    check("only one morechildren call for one batch", thread.more_calls == 1)


def test_filters(cfg: Config) -> None:
    print("\nfilters: light + collapse chatter")
    children = [
        comment("keep1", "This is a substantive answer with plenty of detail.", 50),
        comment("bot1", "^(I am a bot, beep boop)", 30, author="AutoModerator"),
        comment("chatter", "this", 2),
        comment("chatter2", "lol", 40),  # short but well-scored: kept
        comment("downvoted", "A wrong but long enough answer to pass length.", -5),
        comment("gone", "[deleted]", 1,
                replies=[comment("orphan", "A valuable reply under a deleted parent.", 25,
                                 parent="t1_gone")]),
        comment("gone2", "[removed]", 1),
        comment("pinned", "Welcome to the subreddit! Please read the rules before posting.",
                1, author="qualityvote2", stickied=True, distinguished="moderator"),
    ]
    thread = fetch_thread("test01", FakeHttp(children), cfg.reddit, log=lambda *a: None)
    filter_thread(thread, cfg.filters)
    ids = {c.id for c in thread.all_comments()}

    check("bot dropped", "bot1" not in ids)
    check("short low-score chatter dropped", "chatter" not in ids)
    check("short high-score comment kept", "chatter2" in ids)
    check("downvoted comment dropped", "downvoted" not in ids)
    check("childless deleted comment dropped", "gone2" not in ids)
    check("pinned mod boilerplate dropped", "pinned" not in ids)
    check("deleted parent kept as stub", "gone" in ids)
    check("reply under deleted parent survives", "orphan" in ids)

    stubs = [c for c in thread.all_comments() if c.is_stub]
    check("stub flagged, not counted as content",
          len(stubs) == 1 and stubs[0].id == "gone"
          and "gone" not in {c.id for c in kept_comments(thread)})


def test_render(cfg: Config) -> None:
    print("\nrendering")
    children = [
        comment("c1", "Top level answer.", 120,
                replies=[comment("c1r1", "First level reply.", 30, parent="t1_c1",
                                 replies=[comment("c1r2", "Second level reply.", 10,
                                                  parent="t1_c1r1",
                                                  replies=[comment("c1r3", "Third level reply goes deep.",
                                                                   5, parent="t1_c1r2")])])]),
    ]
    thread = fetch_thread("test01", FakeHttp(children), cfg.reddit, log=lambda *a: None)
    filter_thread(thread, cfg.filters)
    text = render(thread, version=2, captured_at=now_utc(),
                  changelog=["- **v1** — 2026-09-01 · 1 comments · 3 words"])

    check("frontmatter has post_id", "post_id: test01" in text)
    check("frontmatter has version", "version: 2" in text)
    check("title heading present", "# How I actually use AI tools daily" in text)
    check("post body included", "Here is my workflow." in text)
    check("top-level comment is a heading", "### u/user_c1 — 120 pts" in text)
    check("depth-1 reply single-quoted", "\n> **u/user_c1r1**" in text)
    check("depth-2 reply double-quoted", "\n>> **u/user_c1r2**" in text)
    check("depth-3 reply triple-quoted", "\n>>> **u/user_c1r3**" in text)
    check("changelog carried forward", "- **v1** — 2026-09-01" in text)
    check("slug is filename-safe", slugify("How I actually use AI tools daily!?")
          == "how-i-actually-use-ai-tools-daily")


def _metrics(n_comments: int, words_each: int, top_score: int, post_score: int,
             prefix: str = "c", start: int = 0, default_score: int = 5) -> Metrics:
    m = Metrics(post_score=post_score)
    for i in range(start, start + n_comments):
        cid = f"{prefix}{i}"
        m.comment_ids.add(cid)
        m.words_by_id[cid] = words_each
        m.scores_by_id[cid] = default_score
        m.authors_by_id[cid] = f"user{i}"
        m.kept_words += words_each
    if m.comment_ids:
        best = f"{prefix}{start}"
        m.scores_by_id[best] = top_score
        m.top_comment_score = top_score
    m.kept_comments = len(m.comment_ids)
    return m


def test_gates(cfg: Config) -> None:
    print("\nversion promotion gates")
    db = Path(tempfile.mkdtemp()) / "test.db"
    conn = connect(db)
    vcfg = cfg.versioning

    v1 = _metrics(20, 50, top_score=100, post_score=200)   # 20 comments, 1000 words
    add_version(conn, "test01", 1, v1, "first capture")
    previous = latest_version(conn, "test01")
    later = now_utc() + timedelta(hours=24)

    check("first capture always promotes",
          decide(None, v1, vcfg, version_count=0, now=now_utc()).promote)

    tiny = _metrics(3, 30, 5, 210, start=100)
    tiny.comment_ids |= v1.comment_ids
    tiny.words_by_id.update(v1.words_by_id)
    tiny.kept_words += v1.kept_words
    d = decide(previous, tiny, vcfg, version_count=1, now=later)
    check("+3 small comments does NOT promote", not d.promote, d.rule)

    grown = _metrics(40, 40, 30, 210, start=200)  # +1600 words
    grown.comment_ids |= v1.comment_ids
    grown.words_by_id.update(v1.words_by_id)
    grown.kept_words += v1.kept_words
    d = decide(previous, grown, vcfg, version_count=1, now=later)
    check("+40 substantive comments promotes (rule a)", d.promote and d.rule == "a", d.rule)
    check("delta counted against previous id set", d.new_comments == 40, str(d.new_comments))

    hot = _metrics(1, 20, top_score=500, post_score=210, start=300)
    hot.comment_ids |= v1.comment_ids
    hot.words_by_id.update(v1.words_by_id)
    hot.kept_words += v1.kept_words
    d = decide(previous, hot, vcfg, version_count=1, now=later)
    check("one new high-score answer promotes (rule b)", d.promote and d.rule == "b", d.rule)
    check("note names the new top answer", "new top answer" in d.note, d.note)

    viral = _metrics(6, 20, top_score=20, post_score=900, start=400)
    viral.comment_ids |= v1.comment_ids
    viral.words_by_id.update(v1.words_by_id)
    viral.kept_words += v1.kept_words
    d = decide(previous, viral, vcfg, version_count=1, now=later)
    check("early-capture post going viral promotes (rule d)", d.promote and d.rule == "d", d.rule)

    d = decide(previous, grown, vcfg, version_count=1, now=now_utc() + timedelta(hours=2))
    check("12h gate blocks a rapid re-check", not d.promote and "since v1" in d.blocked_by,
          d.blocked_by)
    d = decide(previous, grown, vcfg, version_count=1,
               now=now_utc() + timedelta(hours=2), forced=True)
    check("/force bypasses the 12h gate", d.promote, d.blocked_by)

    d = decide(previous, grown, vcfg, version_count=vcfg.max_versions, now=later)
    check("version cap respected", not d.promote and "cap" in d.blocked_by, d.blocked_by)

    conn.close()


def test_relative_growth_rule(cfg: Config) -> None:
    """Rule e: small threads in low-score subreddits, where every score is 1."""
    print("\nrule e: relative growth on small captures")
    db = Path(tempfile.mkdtemp()) / "test.db"
    conn = connect(db)
    vcfg = cfg.versioning
    later = now_utc() + timedelta(hours=24)

    # A real case: r/claudeskills post captured at 4 comments / 68 words, all 1 pt.
    small = _metrics(4, 17, top_score=1, post_score=1, default_score=1)
    add_version(conn, "small01", 1, small, "first capture")
    previous = latest_version(conn, "small01")

    grown = _metrics(11, 29, top_score=1, post_score=1, start=50, default_score=1)  # +319 words
    grown.comment_ids |= small.comment_ids
    grown.words_by_id.update(small.words_by_id)
    grown.kept_words += small.kept_words
    d = decide(previous, grown, vcfg, version_count=1, now=later)
    check("5x growth on a tiny thread promotes (rule e)", d.promote and d.rule == "e", d.rule)
    check("no bogus 'top answer' when everything is 1 pt",
          "top answer" not in d.note, d.note)

    # Same absolute delta on a big thread must NOT promote via rule e.
    big = _metrics(60, 50, top_score=200, post_score=500)   # 3,000 words
    add_version(conn, "big01", 1, big, "first capture")
    big_prev = latest_version(conn, "big01")
    big_grown = _metrics(11, 29, top_score=3, post_score=520, start=90)  # +319 words
    big_grown.comment_ids |= big.comment_ids
    big_grown.words_by_id.update(big.words_by_id)
    big_grown.kept_words += big.kept_words
    d = decide(big_prev, big_grown, vcfg, version_count=1, now=later)
    check("same delta on a big thread does NOT promote", not d.promote, d.rule)

    # A big thread that genuinely doubles still promotes.
    doubled = _metrics(60, 50, top_score=3, post_score=520, start=200)  # +3,000 words
    doubled.comment_ids |= big.comment_ids
    doubled.words_by_id.update(big.words_by_id)
    doubled.kept_words += big.kept_words
    d = decide(big_prev, doubled, vcfg, version_count=1, now=later)
    check("big thread doubling still promotes", d.promote, d.blocked_by)
    conn.close()


def test_offline_warning(cfg: Config) -> None:
    """The bot must flag a gap long enough for Telegram to have dropped shares."""
    print("\noffline-gap warning")
    import bot as bot_module
    from store import set_meta

    db = Path(tempfile.mkdtemp()) / "test.db"
    conn = connect(db)
    sent: list[str] = []

    class FakeTg:
        def send(self, chat_id, text, reply_to=None):
            sent.append(text)

    class FakeLog:
        def warning(self, *a): pass

    bot_module.warn_offline_gap(conn, FakeTg(), cfg, "123", FakeLog())
    check("no warning on first run (nothing missed)", not sent)

    set_meta(conn, "last_seen", iso(now_utc() - timedelta(hours=3)))
    bot_module.warn_offline_gap(conn, FakeTg(), cfg, "123", FakeLog())
    check("no warning after a short absence", not sent)

    set_meta(conn, "last_seen", iso(now_utc() - timedelta(hours=30)))
    bot_module.warn_offline_gap(conn, FakeTg(), cfg, "123", FakeLog())
    check("warns after 30h offline", len(sent) == 1, str(len(sent)))
    check("warning tells you what to do",
          sent and "forward it again" in sent[0], sent[0][:60] if sent else "")
    conn.close()


class FakeArcticHttp:
    """Arctic Shift serves the same JSON shape as Reddit, over plain GETs."""

    def __init__(self, post, tree):
        self.post = post
        self.tree = tree

    def json_get(self, url, timeout=None):
        if "/posts/ids" in url:
            return {"data": [self.post]}
        return {"data": self.tree}


def test_arctic_backend(cfg: Config) -> None:
    print("\narctic shift backend")
    import backend_arctic

    post = {
        "id": "1w6t678", "title": "A thread from the archive", "author": "someone",
        "subreddit": "ChatGPTPro", "selftext": "Body text.", "score": 63,
        "upvote_ratio": 0.84, "num_comments": 35, "created_utc": 1_756_900_000,
        "permalink": "/r/ChatGPTPro/comments/1w6t678/x/", "is_self": True,
        "link_flair_text": "Discussion",
    }
    tree = [
        comment("t1a", "An archived answer with a decent amount of text in it.", 32,
                replies=[comment("t1b", "An archived reply carrying more detail.", 12,
                                 parent="t1_t1a")]),
        comment("t1c", "Another archived top level comment worth keeping.", 20),
    ]
    thread = backend_arctic.fetch_thread(
        "1w6t678", FakeArcticHttp(post, tree), cfg.reddit, cfg.arctic, log=lambda *a: None)

    check("post metadata mapped", thread.title == "A thread from the archive"
          and thread.score == 63 and thread.subreddit == "ChatGPTPro")
    check("permalink absolutised", thread.permalink.startswith("https://www.reddit.com/r/"))
    check("nested replies preserved", {c.id for c in thread.all_comments()}
          == {"t1a", "t1b", "t1c"})
    by_id = {c.id: c for c in thread.all_comments()}
    check("reply depth correct", by_id["t1b"].depth == 1)
    check("scores carried through", by_id["t1a"].score == 32)


class FakePullPushHttp:
    """PullPush returns flat search results, paged 100 at a time."""

    def __init__(self, post, flat):
        self.post = post
        self.flat = flat

    def json_get(self, url, timeout=None):
        if "/submission/" in url:
            return {"data": [self.post] if self.post else []}
        after = 0
        if "after=" in url:
            after = int(url.split("after=")[1].split("&")[0])
        page = [c for c in self.flat if c["created_utc"] > after]
        return {"data": sorted(page, key=lambda c: c["created_utc"])[:100]}


def test_pullpush_backend(cfg: Config) -> None:
    print("\npullpush backend")
    import backend_pullpush

    post = {
        "id": "pp01", "title": "Fallback thread", "author": "someone",
        "subreddit": "ClaudeAI", "selftext": "Body.", "score": 30, "num_comments": 3,
        "created_utc": 1_790_000_000, "permalink": "/r/ClaudeAI/comments/pp01/x/",
        "is_self": True,
    }
    # 250 comments forces three pages; several share a timestamp across a page edge.
    flat = [{"id": f"c{i}", "parent_id": "t3_pp01", "body": f"Top level answer {i}.",
             "score": 3, "author": "a", "created_utc": 1_790_000_000 + i // 3}
            for i in range(250)]
    flat.append({"id": "r1", "parent_id": "t1_c5", "body": "A reply.", "score": 2,
                 "author": "b", "created_utc": 1_790_001_000})
    flat.append({"id": "r2", "parent_id": "t1_r1", "body": "Deeper.", "score": 1,
                 "author": "c", "created_utc": 1_790_001_001})
    flat.append({"id": "o1", "parent_id": "t1_gone", "body": "Orphan.", "score": 1,
                 "author": "d", "created_utc": 1_790_001_002})
    thread = backend_pullpush.fetch_thread(
        "pp01", FakePullPushHttp(post, flat), cfg.reddit, cfg.pullpush, log=lambda *a: None)
    by_id = {c.id: c for c in thread.all_comments()}
    check("all pages fetched, none lost at page edges", len(by_id) == 253, str(len(by_id)))
    check("flat list rebuilt into a tree", by_id["r2"].depth == 2
          and by_id["r1"] in by_id["c5"].replies)
    check("orphaned reply kept at top level", by_id["o1"] in thread.comments)
    check("post metadata mapped", thread.title == "Fallback thread"
          and thread.permalink.startswith("https://www.reddit.com/r/"))

    try:
        backend_pullpush.fetch_thread("pp01", FakePullPushHttp(None, flat), cfg.reddit,
                                      cfg.pullpush, log=lambda *a: None)
        check("unindexed post raises", False)
    except backend_pullpush.PullPushUnavailable:
        check("unindexed post raises", True)


def test_backend_fallback(cfg: Config) -> None:
    """Arctic down -> PullPush serves it; all down -> a transient failure."""
    print("\nbackend fallback")
    import urllib.error
    import backends
    from http_util import is_transient

    def http_error(code):
        return urllib.error.HTTPError("https://x", code, "err", {}, None)

    class ArcticDown(FakePullPushHttp):
        def json_get(self, url, timeout=None):
            if "arctic-shift" in url:
                raise http_error(500)
            return super().json_get(url, timeout)

    post = {"id": "pp02", "title": "T", "subreddit": "s", "permalink": "/r/s/comments/pp02/t/"}
    flat = [{"id": "c1", "parent_id": "t3_pp02", "body": "hi", "score": 1,
             "created_utc": 1_790_000_000}]
    local = Config()
    local.reddit.backend = "auto"
    local.reddit.client_id = local.reddit.client_secret = ""
    check("auto without credentials tries both archives",
          backends.backend_order(local) == ["arctic", "pullpush"])
    thread = backends.get_thread("pp02", ArcticDown(post, flat), local, log=lambda *a: None)
    check("falls back to pullpush when arctic 500s", thread.title == "T")

    class AllDown:
        def json_get(self, url, timeout=None):
            raise http_error(503)
    try:
        backends.get_thread("pp02", AllDown(), local, log=lambda *a: None)
        check("all sources down raises", False)
    except backends.SourcesUnavailable as exc:
        check("all sources down is transient", exc.transient and is_transient(exc))

    check("404 is not transient", not is_transient(http_error(404)))
    check("500 and 429 are transient", is_transient(http_error(500)) and is_transient(http_error(429)))
    check("network error is transient", is_transient(urllib.error.URLError("getaddrinfo failed")))


def test_retry_queue(cfg: Config) -> None:
    """A share that fails on an outage is queued, retried, and answered later."""
    print("\nretry queue")
    import bot as bot_module
    from backends import SourcesUnavailable
    from pipeline import Outcome
    from store import all_pending
    from telegram import Incoming
    import urllib.error

    conn = connect(Path(tempfile.mkdtemp()) / "test.db")
    sent: list[str] = []

    class FakeTg:
        def send(self, chat_id, text, reply_to=None):
            sent.append(text)

    class FakeLog:
        def info(self, *a): pass
        def warning(self, *a): pass
        def error(self, *a): pass
        def exception(self, *a): pass

    down = SourcesUnavailable([("arctic", urllib.error.HTTPError("u", 500, "e", {}, None))])
    state = {"fail": True}

    def fake_process_url(url, *a, **k):
        if state["fail"]:
            raise down
        return Outcome("created", "✅ v1 — saved", None, 1)

    original = bot_module.process_url
    bot_module.process_url = fake_process_url
    try:
        url = "https://www.reddit.com/r/x/s/abc"
        msg = Incoming(chat_id="123", text=url, message_id=7, sender="me")
        bot_module.handle_message(msg, cfg, conn, None, FakeTg(), FakeLog())
        check("outage queues the link", len(all_pending(conn)) == 1)
        check("user told it is queued", sent and "Queued" in sent[-1], sent[-1][:80])

        bot_module.retry_pending(cfg, conn, None, FakeTg(), FakeLog())
        check("not retried before it is due", len(sent) == 1)

        conn.execute("UPDATE pending SET next_try = ?", (iso(now_utc() - timedelta(minutes=1)),))
        bot_module.retry_pending(cfg, conn, None, FakeTg(), FakeLog())
        row = all_pending(conn)[0]
        check("failed retry reschedules with backoff", row["attempts"] == 2 and len(sent) == 1)

        state["fail"] = False
        conn.execute("UPDATE pending SET next_try = ?", (iso(now_utc() - timedelta(minutes=1)),))
        bot_module.retry_pending(cfg, conn, None, FakeTg(), FakeLog())
        check("success clears the queue", not all_pending(conn))
        check("success replies with the saved note", "saved on retry" in sent[-1], sent[-1])

        state["fail"] = True
        bot_module.handle_message(msg, cfg, conn, None, FakeTg(), FakeLog())
        old = iso(now_utc() - timedelta(hours=cfg.retry.give_up_hours + 1))
        conn.execute("UPDATE pending SET first_tried = ?, next_try = ?", (old, old))
        bot_module.retry_pending(cfg, conn, None, FakeTg(), FakeLog())
        check("gives up after the window", not all_pending(conn) and "Gave up" in sent[-1],
              sent[-1][:80])
    finally:
        bot_module.process_url = original
    conn.close()


def test_pipeline(cfg: Config) -> None:
    """Full capture -> re-check -> archive cycle against a temp output folder."""
    print("\npipeline: capture, version bump, archive")
    from pipeline import process

    sandbox = Path(tempfile.mkdtemp())
    cfg.paths.output_dir = sandbox / "notes"
    cfg.paths.state_dir = sandbox / "state"
    cfg.reddit.backend = "official"  # FakeHttp speaks the official API shape
    conn = connect(cfg.paths.db_path)

    v1_children = [
        comment(f"c{i}", "A substantive answer with enough words to count as content here.", 20 + i)
        for i in range(5)
    ]
    outcome = process("test01", "https://reddit.com/x", cfg, conn,
                      FakeHttp(v1_children), log=lambda *a: None)
    check("first capture writes a file", outcome.kind == "created" and outcome.path.exists(),
          outcome.kind)
    check("filename carries the post id", outcome.path.name.endswith("--test01.md"),
          outcome.path.name)

    # Same thread again: nothing new, so no version even when forced.
    outcome = process("test01", "https://reddit.com/x", cfg, conn,
                      FakeHttp(v1_children), force=True, log=lambda *a: None)
    check("identical re-check does not bump", outcome.kind == "unchanged", outcome.kind)

    # Thread grows substantially.
    v2_children = v1_children + [
        comment(f"n{i}", "A new late answer that adds real detail to the discussion.", 15)
        for i in range(40)
    ]
    outcome = process("test01", "https://reddit.com/x", cfg, conn,
                      FakeHttp(v2_children), force=True, log=lambda *a: None)
    check("substantial growth bumps to v2", outcome.kind == "updated" and outcome.version == 2,
          f"{outcome.kind} v{outcome.version}")

    archived = cfg.paths.versions_dir / f"{outcome.path.stem}.v1.md"
    check("previous version archived", archived.exists(), str(archived))

    text = outcome.path.read_text(encoding="utf-8")
    check("live file says version 2", "version: 2" in text)
    check("changelog lists both versions",
          "- **v1**" in text and "- **v2**" in text)
    check("changelog names what changed", "+40 comments" in text,
          text.split("## Changelog")[-1][:200])
    check("archived copy still says version 1",
          "version: 1" in archived.read_text(encoding="utf-8"))
    conn.close()


def main() -> int:
    cfg = Config.load()
    test_expansion(cfg)
    test_filters(cfg)
    test_render(cfg)
    test_gates(cfg)
    test_relative_growth_rule(cfg)
    test_offline_warning(cfg)
    test_arctic_backend(cfg)
    test_pullpush_backend(cfg)
    test_backend_fallback(cfg)
    test_retry_queue(cfg)
    test_pipeline(cfg)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
