"""Thread -> markdown.

Top-level comments become `###` headings so editor outlines are navigable;
replies nest as blockquotes to three levels, after which they flatten with a
depth marker rather than sliding off the right edge of the screen.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fetch import Comment, Thread
from filters import count_words, kept_comments

MAX_QUOTE_DEPTH = 3
SLUG_MAX = 60


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if len(slug) > SLUG_MAX:
        slug = slug[:SLUG_MAX].rsplit("-", 1)[0] or slug[:SLUG_MAX]
    return slug or "untitled"


def filename_for(thread: Thread) -> str:
    return f"{slugify(thread.title)}--{thread.post_id}.md"


def _local(ts: float) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="minutes")


def _yaml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _quote(text: str, level: int) -> str:
    prefix = ">" * level + " "
    lines = []
    for line in text.strip().splitlines():
        lines.append((prefix + line).rstrip() if line.strip() else prefix.rstrip())
    return "\n".join(lines)


def _byline(comment: Comment) -> str:
    bits = [f"**u/{comment.author}**", f"{comment.score} pts"]
    if comment.is_submitter:
        bits.append("OP")
    if comment.distinguished == "moderator":
        bits.append("mod")
    when = _local(comment.created_utc)
    if when:
        bits.append(when[:10])
    return " · ".join(bits)


def _render_comment(comment: Comment, out: list[str], depth: int) -> None:
    body = "*[removed — kept for the replies below]*" if comment.is_stub else comment.body.strip()

    if depth == 0:
        header = f"### u/{comment.author} — {comment.score} pts"
        if comment.is_submitter:
            header += " (OP)"
        out.append(header)
        out.append("")
        out.append(body)
        out.append("")
    else:
        level = min(depth, MAX_QUOTE_DEPTH)
        marker = "" if depth <= MAX_QUOTE_DEPTH else f" · ↳ depth {depth}"
        out.append(_quote(_byline(comment) + marker, level))
        out.append(">" * level)
        out.append(_quote(body, level))
        out.append("")

    for reply in comment.replies:
        _render_comment(reply, out, depth + 1)


def render(thread: Thread, *, version: int, captured_at: datetime,
           changelog: list[str]) -> str:
    comments = kept_comments(thread)
    words = sum(count_words(c.body) for c in comments)

    front = [
        "---",
        f"title: {_yaml_str(thread.title)}",
        f"url: {thread.permalink}",
        f"post_id: {thread.post_id}",
        f"subreddit: {thread.subreddit}",
        f"author: {thread.author}",
        f"posted: {_local(thread.created_utc)}",
        f"captured: {captured_at.astimezone().isoformat(timespec='minutes')}",
        f"version: {version}",
        f"post_score: {thread.score}",
        f"upvote_ratio: {thread.upvote_ratio}",
        f"comments_kept: {len(comments)}",
        f"comments_reported: {thread.num_comments}",
        f"words: {words}",
        f"tags: [reddit, {thread.subreddit}]",
        *(["removed_by_moderator: true"] if thread.removed else []),
        "---",
        "",
    ]

    out = list(front)
    out.append(f"# {thread.title}")
    out.append("")
    meta = f"[r/{thread.subreddit}]({thread.permalink}) · u/{thread.author} · {thread.score} pts"
    if thread.flair:
        meta += f" · {thread.flair}"
    out.append(meta)
    out.append("")

    if thread.removed:
        out.append("> ⚠️ This post was removed on Reddit after it was posted. "
                   "The title is recovered from its URL and the body may be gone; "
                   "the comments below were archived before removal.")
        out.append("")

    if thread.selftext.strip():
        out.append(thread.selftext.strip())
        out.append("")
    elif thread.link_url and not thread.is_self:
        out.append(f"Link post → <{thread.link_url}>")
        out.append("")

    out.append(f"## Comments ({len(comments)})")
    out.append("")
    if thread.truncated:
        out.append("> ⚠️ Reddit capped comment expansion for this thread; "
                   "some deep replies may be missing.")
        out.append("")

    for comment in thread.comments:
        _render_comment(comment, out, 0)
        out.append("---")
        out.append("")

    out.append("## Changelog")
    out.append("")
    out.extend(changelog)
    out.append("")
    return "\n".join(out)


def changelog_line(version: int, when: datetime, comments: int, words: int,
                   note: str = "") -> str:
    stamp = when.astimezone().strftime("%Y-%m-%d")
    line = f"- **v{version}** — {stamp} · {comments:,} comments · {words:,} words"
    if note:
        line += f" · {note}"
    return line
