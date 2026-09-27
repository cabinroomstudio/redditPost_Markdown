"""PullPush backend: a second no-credential archive, used when Arctic Shift is down.

PullPush serves Pushshift-style search results: the post and its comments come
back as flat lists of Reddit objects (no `replies` nesting), 100 at a time. The
tree is rebuilt here from each comment's parent_id.

Trade-offs versus Arctic Shift: scores are as of PullPush's scrape and can be
stale, and brand-new posts may have comments indexed before the post itself. A
missing post is reported as unavailable so the caller can retry later rather
than save a note with no title or body.
"""
from __future__ import annotations

import urllib.parse

from config import PullPushCfg, RedditCfg
from fetch import Comment, Thread, _mk_comment, resolve_title
from http_util import RedditHttp

PAGE_SIZE = 100


class PullPushUnavailable(RuntimeError):
    pass


def _post(post_id: str, http: RedditHttp, cfg: PullPushCfg) -> dict:
    query = urllib.parse.urlencode({"ids": post_id})
    payload = http.json_get(f"{cfg.base}/reddit/search/submission/?{query}", timeout=cfg.timeout)
    data = payload.get("data") or []
    if not data:
        raise PullPushUnavailable(f"PullPush has not indexed post {post_id} yet")
    return data[0]


def _comments(post_id: str, http: RedditHttp, cfg: PullPushCfg) -> list[dict]:
    """Page through the flat comment list oldest-first.

    `after` is exclusive, so each page restarts one second before the last seen
    timestamp and duplicates are dropped - comments sharing a second are not lost.
    """
    seen: dict[str, dict] = {}
    after = 0
    for _ in range(cfg.max_pages):
        params = {"link_id": post_id, "size": PAGE_SIZE, "sort": "asc", "sort_type": "created_utc"}
        if after:
            params["after"] = after
        payload = http.json_get(
            f"{cfg.base}/reddit/search/comment/?{urllib.parse.urlencode(params)}",
            timeout=cfg.timeout)
        page = payload.get("data") or []
        fresh = [c for c in page if c.get("id") and c["id"] not in seen]
        for c in fresh:
            seen[c["id"]] = c
        if len(page) < PAGE_SIZE or not fresh:
            break
        after = int(max(float(c.get("created_utc") or 0) for c in page)) - 1
    return list(seen.values())


def _build(flat: list[dict]) -> tuple[list[Comment], dict[str, Comment]]:
    index: dict[str, Comment] = {}
    for data in flat:
        comment = _mk_comment(data, 0)
        index[comment.fullname] = comment
    roots: list[Comment] = []
    for comment in index.values():
        parent = index.get(comment.parent_id)
        if parent is not None:
            parent.replies.append(comment)
        else:
            # Top level - or a reply whose parent the archive lacks: keep, don't drop.
            roots.append(comment)

    def set_depth(nodes: list[Comment], depth: int) -> None:
        for node in nodes:
            node.depth = depth
            set_depth(node.replies, depth + 1)
    set_depth(roots, 0)
    return roots, index


def fetch_thread(post_id: str, http: RedditHttp, reddit_cfg: RedditCfg,
                 pp_cfg: PullPushCfg, log=print) -> Thread:
    post = _post(post_id, http, pp_cfg)
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

    flat = _comments(post_id, http, pp_cfg)
    thread.comments, index = _build(flat)
    count = len(index)
    thread.num_comments = max(thread.num_comments, count)
    log(f"  pullpush: {count} comments (reddit reports {thread.num_comments})")
    if len(flat) >= pp_cfg.max_pages * PAGE_SIZE:
        thread.truncated = True
    return thread
