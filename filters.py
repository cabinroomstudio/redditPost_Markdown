"""Comment filtering: "light + collapse chatter".

Everything at every depth is kept except noise. A dropped comment that still has
surviving descendants is retained as a stub, so an answer never loses the
question it was replying to.
"""
from __future__ import annotations

from config import FilterCfg
from fetch import Comment, Thread

DELETED_BODIES = {"[deleted]", "[removed]", "[unavailable]", "[ Removed by Reddit ]"}


def is_bot(author: str, cfg: FilterCfg) -> bool:
    lowered = author.lower()
    if any(lowered == known.lower() for known in cfg.bot_authors):
        return True
    return any(lowered.endswith(suffix.lower()) for suffix in cfg.bot_suffixes)


def is_deleted(comment: Comment) -> bool:
    body = comment.body.strip()
    return body in DELETED_BODIES or (not body and comment.author == "[deleted]")


def is_mod_boilerplate(comment: Comment, cfg: FilterCfg) -> bool:
    """Pinned mod notices: 'welcome to r/x', vote prompts, rule reminders.

    Catches subreddit housekeeping bots generically, without having to know each
    one by name - they are almost always stickied AND mod-distinguished.
    """
    return cfg.drop_stickied_mod and comment.stickied and comment.distinguished == "moderator"


def keeps_on_merit(comment: Comment, cfg: FilterCfg) -> bool:
    """Whether this comment survives on its own, ignoring its replies."""
    if is_deleted(comment):
        return False
    if is_bot(comment.author, cfg):
        return False
    if is_mod_boilerplate(comment, cfg):
        return False
    if comment.score < cfg.min_score:
        return False
    body = comment.body.strip()
    if len(body) < cfg.short_len and comment.score < cfg.short_min_score:
        return False
    return True


def _filter(comments: list[Comment], cfg: FilterCfg) -> list[Comment]:
    kept: list[Comment] = []
    for comment in comments:
        comment.replies = _filter(comment.replies, cfg)
        if keeps_on_merit(comment, cfg):
            comment.is_stub = False
            kept.append(comment)
        elif comment.replies:
            comment.is_stub = True  # chain preservation
            kept.append(comment)
    return kept


def filter_thread(thread: Thread, cfg: FilterCfg) -> Thread:
    """Filter in place and return the thread, for convenient chaining."""
    thread.comments = _filter(thread.comments, cfg)
    return thread


def count_words(text: str) -> int:
    return len(text.split())


def kept_comments(thread: Thread) -> list[Comment]:
    """Substantive comments only - stubs are scaffolding, not content."""
    return [c for c in thread.all_comments() if not c.is_stub]
