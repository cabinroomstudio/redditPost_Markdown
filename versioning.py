"""Deciding whether a re-check earned a new version.

The point of the gates is that "three more comments" must not produce a v2,
while "the thread finally took off and the real answers arrived" must. Deltas
are computed against the exact comment-id set of the previous version.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from config import VersionCfg
from fetch import Thread
from filters import count_words, kept_comments
from store import parse, unpack_ids


@dataclass
class Metrics:
    kept_comments: int = 0
    kept_words: int = 0
    top_comment_score: int = 0
    post_score: int = 0
    comment_ids: set[str] = field(default_factory=set)
    words_by_id: dict[str, int] = field(default_factory=dict)
    scores_by_id: dict[str, int] = field(default_factory=dict)
    authors_by_id: dict[str, str] = field(default_factory=dict)


@dataclass
class Decision:
    promote: bool
    rule: str            # "a".."e", "first", or "" when blocked
    note: str            # human summary for the changelog / Telegram
    new_comments: int = 0
    new_words: int = 0
    best_new_score: int = 0
    best_new_author: str = ""
    blocked_by: str = ""  # why not, when promote is False


def measure(thread: Thread) -> Metrics:
    metrics = Metrics(post_score=thread.score)
    for comment in kept_comments(thread):
        words = count_words(comment.body)
        metrics.comment_ids.add(comment.id)
        metrics.words_by_id[comment.id] = words
        metrics.scores_by_id[comment.id] = comment.score
        metrics.authors_by_id[comment.id] = comment.author
        metrics.kept_words += words
        metrics.top_comment_score = max(metrics.top_comment_score, comment.score)
    metrics.kept_comments = len(metrics.comment_ids)
    return metrics


def _summarise(new_comments: int, new_words: int, best_score: int,
               best_author: str, post_delta: int) -> str:
    bits = []
    if new_comments:
        bits.append(f"+{new_comments} comments")
    if new_words:
        bits.append(f"+{new_words:,} words")
    # In low-score subreddits everything sits at 1 point; calling that a "top
    # answer" is noise, so only say it when the score actually means something.
    if best_score >= 5 and best_author:
        bits.append(f"new top answer by u/{best_author} ({best_score} pts)")
    if post_delta > 0:
        bits.append(f"post +{post_delta:,} pts")
    return ", ".join(bits) or "no measurable change"


def decide(previous, metrics: Metrics, cfg: VersionCfg, *,
           version_count: int, now: datetime, forced: bool = False) -> Decision:
    """previous is a sqlite Row from store.latest_version, or None for a first capture."""
    if previous is None:
        return Decision(True, "first", "first capture",
                        new_comments=metrics.kept_comments,
                        new_words=metrics.kept_words)

    prev_ids = unpack_ids(previous["comment_ids"])
    new_ids = metrics.comment_ids - prev_ids
    new_comments = len(new_ids)
    new_words = sum(metrics.words_by_id.get(cid, 0) for cid in new_ids)
    best_id = max(new_ids, key=lambda cid: metrics.scores_by_id.get(cid, 0), default=None)
    best_new_score = metrics.scores_by_id.get(best_id, 0) if best_id else 0
    best_new_author = metrics.authors_by_id.get(best_id, "") if best_id else ""
    post_delta = metrics.post_score - previous["post_score"]

    note = _summarise(new_comments, new_words, best_new_score, best_new_author, post_delta)
    base = Decision(False, "", note, new_comments, new_words, best_new_score, best_new_author)

    # ---- hard gates ----
    if version_count >= cfg.max_versions:
        base.blocked_by = f"version cap ({cfg.max_versions}) reached"
        return base
    if not forced:
        last = parse(previous["created_at"])
        if last is not None:
            hours = (now - last).total_seconds() / 3600
            if hours < cfg.min_hours_between:
                base.blocked_by = f"only {hours:.1f}h since v{previous['version']}"
                return base

    # ---- value gate: any one rule is enough ----
    prev_words = previous["kept_words"] or 0
    prev_top = previous["top_comment_score"] or 0
    prev_post_score = previous["post_score"] or 0

    if new_words >= cfg.rule_a_new_words and new_words >= cfg.rule_a_ratio * max(prev_words, 1):
        base.promote, base.rule = True, "a"
        return base

    if best_new_score >= max(cfg.rule_b_min_score, cfg.rule_b_ratio * prev_top):
        base.promote, base.rule = True, "b"
        return base

    if new_comments >= cfg.rule_c_new_comments and new_words >= cfg.rule_c_new_words:
        base.promote, base.rule = True, "c"
        return base

    if (metrics.post_score >= cfg.rule_d_score_multiple * max(prev_post_score, 1)
            and new_comments >= cfg.rule_d_new_comments):
        base.promote, base.rule = True, "d"
        return base

    # Rule e catches the case the absolute floors miss: a small thread that grew
    # several times over. In niche subreddits every comment sits at 1 point, so
    # the score-based rules are blind there and totals never reach the floors.
    if (metrics.kept_words >= cfg.rule_e_growth_multiple * max(prev_words, 1)
            and new_words >= cfg.rule_e_new_words
            and new_comments >= cfg.rule_e_new_comments):
        base.promote, base.rule = True, "e"
        return base

    base.blocked_by = "change below thresholds"
    return base


def next_check(check_index: int, first_captured: datetime, cfg: VersionCfg):
    """Next point on the decaying ladder, or None once the window closes."""
    if check_index >= len(cfg.backoff_hours):
        return None
    from datetime import timedelta
    return first_captured + timedelta(hours=cfg.backoff_hours[check_index])
