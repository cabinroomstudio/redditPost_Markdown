"""Arctic Shift backend: full threads with no Reddit credentials.

Arctic Shift is a community archive that serves Reddit data in Reddit's own JSON
shape ({"kind": "t1", "data": {... "replies": {...}}}), so the tree builder in
fetch.py is reused as-is. It also delivers the whole tree in one response - there
are no `more` placeholders to expand.

Verified against live data: comment ids, scores, bodies, authors and nesting all
present; a 177-comment thread returned 195 nodes (it keeps deleted ones) in ~3s.
"""
from __future__ import annotations

import urllib.parse

from config import ArcticCfg, RedditCfg
from fetch import Comment, Thread, _build_tree, resolve_title
from http_util import RedditHttp


class ArcticUnavailable(RuntimeError):
    pass


def _post(post_id: str, http: RedditHttp, cfg: ArcticCfg) -> dict:
    url = f"{cfg.base}/posts/ids?{urllib.parse.urlencode({'ids': post_id})}"
    payload = http.json_get(url, timeout=cfg.timeout)
    data = payload.get("data") or []
    if isinstance(data, dict):
        data = [data]
    if not data:
        raise ArcticUnavailable(
            f"Arctic Shift has no record of post {post_id}. Very new posts can take "
            "a few minutes to be indexed - try again shortly.")
    return data[0]


def _tree(post_id: str, http: RedditHttp, cfg: ArcticCfg) -> list[dict]:
    query = urllib.parse.urlencode({
        "link_id": post_id, "limit": cfg.comment_limit, "md2html": "false"})
    payload = http.json_get(f"{cfg.base}/comments/tree?{query}", timeout=cfg.timeout)
    return payload.get("data") or []


def fetch_thread(post_id: str, http: RedditHttp, reddit_cfg: RedditCfg,
                 arctic_cfg: ArcticCfg, log=print) -> Thread:
    post = _post(post_id, http, arctic_cfg)
    permalink = post.get("permalink") or ""
    if permalink and not permalink.startswith("http"):
        permalink = "https://www.reddit.com" + permalink

    title, removed = resolve_title(post.get("title") or "", permalink)
    thread = Thread(
        post_id=post.get("id", post_id),
        title=title,
        author=post.get("author") or "[unknown]",
        subreddit=post.get("subreddit") or "",
        selftext=post.get("selftext") or "",
        permalink=permalink or f"https://www.reddit.com/comments/{post_id}",
        link_url=post.get("url") or "",
        score=int(post.get("score") or 0),
        upvote_ratio=float(post.get("upvote_ratio") or 0),
        num_comments=int(post.get("num_comments") or 0),
        created_utc=float(post.get("created_utc") or 0),
        is_self=bool(post.get("is_self")),
        flair=post.get("link_flair_text") or "",
        removed=removed,
    )

    index: dict[str, Comment] = {}
    mores: list = []  # Arctic returns the whole tree; nothing to expand
    thread.comments = _build_tree(_tree(post_id, http, arctic_cfg), 0, index, mores)

    count = len(index)
    # The archive's post record can lag its own comment data for brand-new posts,
    # reporting 0 comments while the tree already has some. Trust the larger number.
    thread.num_comments = max(thread.num_comments, count)
    log(f"  arctic: {count} comments (reddit reports {thread.num_comments})")
    # The archive caps very large threads; say so rather than silently truncating.
    if count >= arctic_cfg.comment_limit:
        thread.truncated = True
    return thread
