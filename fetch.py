"""Fetch a full Reddit thread, including everything hidden behind `more` nodes.

Reddit's thread JSON returns roughly the first 200-500 comments and replaces the
rest with `more` placeholders. Expanding those is the difference between an
archive of a QnA thread and just the top of one, so it is done properly here:
pending child ids are pooled across placeholders and sent 100 at a time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from config import RedditCfg
from http_util import RedditHttp

MORECHILDREN_BATCH = 100
REMOVED_TITLE_RE = re.compile(r"^\[\s*(removed|deleted)\b.*\]$", re.I)
SLUG_RE = re.compile(r"/comments/[a-z0-9]+/([^/]+)/?", re.I)


def resolve_title(title: str, permalink: str) -> tuple[str, bool]:
    """Recover a usable title for removed posts.

    A moderator-removed post reports its title as "[ Removed by moderator ]",
    which would otherwise become the filename. The original title survives in the
    permalink slug, and the comments - the reason for saving it - are still there.
    Returns (title, was_removed).
    """
    clean = (title or "").strip()
    if clean and not REMOVED_TITLE_RE.match(clean):
        return clean, False
    match = SLUG_RE.search(permalink or "")
    if match and match.group(1) not in ("_", ""):
        words = match.group(1).replace("_", " ").strip()
        if words:
            return words[0].upper() + words[1:], True
    return clean or "(untitled)", True


@dataclass
class Comment:
    id: str
    author: str
    body: str
    score: int
    created_utc: float
    parent_id: str
    depth: int = 0
    edited: bool = False
    stickied: bool = False
    distinguished: str | None = None
    is_submitter: bool = False
    replies: list["Comment"] = field(default_factory=list)
    is_stub: bool = False  # kept only because a descendant survived filtering

    @property
    def fullname(self) -> str:
        return f"t1_{self.id}"

    def walk(self):
        yield self
        for reply in self.replies:
            yield from reply.walk()


@dataclass
class Thread:
    post_id: str
    title: str
    author: str
    subreddit: str
    selftext: str
    permalink: str
    link_url: str
    score: int
    upvote_ratio: float
    num_comments: int
    created_utc: float
    is_self: bool
    flair: str
    removed: bool = False   # post itself was removed; comments usually survive
    comments: list[Comment] = field(default_factory=list)
    truncated: bool = False   # hit a fetch cap with placeholders still pending
    more_calls: int = 0

    @property
    def fullname(self) -> str:
        return f"t3_{self.post_id}"

    def all_comments(self):
        for comment in self.comments:
            yield from comment.walk()


@dataclass
class _More:
    parent_id: str
    children: list[str]
    is_continuation: bool


def _mk_comment(data: dict[str, Any], depth: int) -> Comment:
    return Comment(
        id=data.get("id", ""),
        author=data.get("author") or "[unknown]",
        body=data.get("body") or "",
        score=int(data.get("score") or 0),
        created_utc=float(data.get("created_utc") or 0),
        parent_id=data.get("parent_id") or "",
        depth=depth,
        edited=bool(data.get("edited")),
        stickied=bool(data.get("stickied")),
        distinguished=data.get("distinguished"),
        is_submitter=bool(data.get("is_submitter")),
    )


def _build_tree(children: list[dict], depth: int, index: dict[str, Comment],
                mores: list[_More]) -> list[Comment]:
    out: list[Comment] = []
    for child in children:
        kind = child.get("kind")
        data = child.get("data") or {}
        if kind == "more":
            ids = [c for c in (data.get("children") or []) if c]
            mores.append(_More(
                parent_id=data.get("parent_id") or "",
                children=ids,
                is_continuation=not ids,
            ))
            continue
        if kind != "t1" or not data.get("id"):
            continue
        comment = _mk_comment(data, depth)
        index[comment.fullname] = comment
        replies = data.get("replies")
        if isinstance(replies, dict):
            comment.replies = _build_tree(
                replies.get("data", {}).get("children", []), depth + 1, index, mores)
        out.append(comment)
    return out


def _attach(things: list[dict], root_fullname: str, roots: list[Comment],
            index: dict[str, Comment], mores: list[_More]) -> int:
    """Splice a flat morechildren response into the tree. Returns comments added."""
    created: list[Comment] = []
    for thing in things:
        kind = thing.get("kind")
        data = thing.get("data") or {}
        if kind == "more":
            ids = [c for c in (data.get("children") or []) if c]
            mores.append(_More(data.get("parent_id") or "", ids, not ids))
            continue
        if kind != "t1" or not data.get("id"):
            continue
        fullname = "t1_" + data["id"]
        if fullname in index:
            continue
        comment = _mk_comment(data, 0)
        index[fullname] = comment
        created.append(comment)

    # Parents usually precede children, but never assume it: loop until stable.
    pending = created
    added = 0
    while pending:
        waiting = {c.fullname for c in pending}
        deferred: list[Comment] = []
        for comment in pending:
            if comment.parent_id == root_fullname:
                comment.depth = 0
                roots.append(comment)
                added += 1
                continue
            parent = index.get(comment.parent_id)
            if parent is None or parent is comment:
                continue  # orphan: parent was never delivered, drop it
            if parent.fullname in waiting:
                deferred.append(comment)
                continue
            comment.depth = parent.depth + 1
            parent.replies.append(comment)
            added += 1
        if len(deferred) == len(pending):
            break
        pending = deferred
    return added


def fetch_thread(post_id: str, http: RedditHttp, cfg: RedditCfg, log=print) -> Thread:
    payload = http.api_get("/comments/" + post_id,
                           {"limit": cfg.comment_limit, "sort": cfg.sort})
    if not isinstance(payload, list) or len(payload) < 2:
        raise RuntimeError("Unexpected thread payload for " + post_id)

    post_children = payload[0].get("data", {}).get("children", [])
    if not post_children:
        raise RuntimeError("Post " + post_id + " not found (deleted or private?)")
    post = post_children[0]["data"]

    permalink = "https://www.reddit.com" + post.get("permalink", "")
    title, removed = resolve_title(post.get("title", ""), permalink)
    thread = Thread(
        post_id=post.get("id", post_id),
        title=title,
        author=post.get("author") or "[unknown]",
        subreddit=post.get("subreddit", ""),
        selftext=post.get("selftext") or "",
        permalink=permalink,
        link_url=post.get("url_overridden_by_dest") or post.get("url") or "",
        score=int(post.get("score") or 0),
        upvote_ratio=float(post.get("upvote_ratio") or 0),
        num_comments=int(post.get("num_comments") or 0),
        created_utc=float(post.get("created_utc") or 0),
        is_self=bool(post.get("is_self")),
        flair=post.get("link_flair_text") or "",
        removed=removed,
    )

    index: dict[str, Comment] = {}
    mores: list[_More] = []
    thread.comments = _build_tree(
        payload[1].get("data", {}).get("children", []), 0, index, mores)

    _expand(thread, mores, index, http, cfg, log)
    return thread


def _expand(thread: Thread, mores: list[_More], index: dict[str, Comment],
            http: RedditHttp, cfg: RedditCfg, log) -> None:
    """Drain `more` placeholders: pooled batches first, deep sub-fetches after."""
    while mores and thread.more_calls < cfg.max_more_calls:
        pool: list[str] = []
        leftover: list[_More] = []
        for more in mores:
            if more.is_continuation:
                leftover.append(more)
            else:
                pool.extend(cid for cid in more.children if "t1_" + cid not in index)
        mores = leftover

        if not pool:
            break
        pool = list(dict.fromkeys(pool))  # de-duplicate, preserve order
        for start in range(0, len(pool), MORECHILDREN_BATCH):
            if thread.more_calls >= cfg.max_more_calls:
                thread.truncated = True
                break
            batch = pool[start:start + MORECHILDREN_BATCH]
            thread.more_calls += 1
            try:
                response = http.api_post("/api/morechildren", {
                    "api_type": "json",
                    "link_id": thread.fullname,
                    "children": ",".join(batch),
                    "sort": cfg.sort,
                    "limit_children": False,
                })
            except Exception as exc:  # one bad batch must not cost us the thread
                log("  morechildren batch failed (" + str(exc) + "); continuing")
                thread.truncated = True
                continue
            data = response.get("json", {}).get("data") or {}
            added = _attach(data.get("things", []), thread.fullname,
                            thread.comments, index, mores)
            log("  +" + str(added) + " comments (call " + str(thread.more_calls)
                + ", " + str(len(batch)) + " ids)")

    # "Continue this thread" placeholders carry no ids; fetch the subtree directly.
    deep_fetches = 0
    for more in mores:
        if not more.is_continuation or not more.parent_id.startswith("t1_"):
            continue
        if deep_fetches >= cfg.max_deep_fetches:
            thread.truncated = True
            break
        parent = index.get(more.parent_id)
        if parent is None:
            continue
        deep_fetches += 1
        try:
            payload = http.api_get(
                "/comments/" + thread.post_id + "/_/" + parent.id,
                {"limit": cfg.comment_limit, "sort": cfg.sort})
        except Exception as exc:
            log("  deep fetch for " + parent.id + " failed (" + str(exc) + ")")
            thread.truncated = True
            continue
        sub_mores: list[_More] = []
        children = payload[1].get("data", {}).get("children", [])
        subtree = _build_tree(children, parent.depth, index, sub_mores)
        for node in subtree:
            if node.id == parent.id:
                parent.replies.extend(node.replies)
            elif node.parent_id == parent.fullname:
                parent.replies.append(node)
        log("  deep-fetched replies under " + parent.id)

    if mores and thread.more_calls >= cfg.max_more_calls:
        thread.truncated = True
