"""Turn whatever the phone shared into a canonical base-36 post id.

The Reddit Android share sheet emits https://www.reddit.com/r/<sub>/s/<code>
with utm_* tracking, not a permalink. The post id is the identity key for
versioning, so resolving happens before anything else.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

from http_util import RedditHttp

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
COMMENTS_RE = re.compile(r"/comments/([a-z0-9]{4,10})(?:/|$)", re.I)
SHORTLINK_RE = re.compile(r"^/([a-z0-9]{4,10})/?$", re.I)
REDDIT_HOSTS = {
    "reddit.com", "www.reddit.com", "old.reddit.com", "np.reddit.com",
    "m.reddit.com", "new.reddit.com", "sh.reddit.com", "i.reddit.com",
    "amp.reddit.com", "redd.it", "www.redd.it",
}


class NotARedditPost(ValueError):
    pass


@dataclass
class ResolvedLink:
    post_id: str
    url: str          # canonical-ish URL after redirects and param stripping
    source_url: str   # exactly what was shared


def extract_urls(text: str) -> list[str]:
    seen, out = set(), []
    for match in URL_RE.findall(text or ""):
        url = match.rstrip(").,;>'\"")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def is_reddit_url(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    except ValueError:
        return False
    return host in REDDIT_HOSTS


def _strip(url: str) -> str:
    parts = urllib.parse.urlparse(url)
    host = parts.netloc.lower().split(":")[0]
    if host in REDDIT_HOSTS and host != "redd.it":
        host = "www.reddit.com"
    return urllib.parse.urlunparse((parts.scheme or "https", host, parts.path, "", "", ""))


def _post_id_from(url: str) -> str | None:
    parts = urllib.parse.urlparse(url)
    host = parts.netloc.lower().split(":")[0]
    match = COMMENTS_RE.search(parts.path)
    if match:
        return match.group(1).lower()
    if host.endswith("redd.it"):
        match = SHORTLINK_RE.match(parts.path)
        if match:
            return match.group(1).lower()
    return None


def resolve(url: str, http: RedditHttp) -> ResolvedLink:
    """Resolve a shared URL to a post id, following redirects only when needed."""
    source = url
    if not is_reddit_url(url):
        raise NotARedditPost(f"Not a Reddit link: {url}")

    post_id = _post_id_from(url)
    if post_id:
        return ResolvedLink(post_id, _strip(url), source)

    # /s/<code> share links and anything else unrecognised: ask Reddit where it goes.
    final = http.final_url(url)
    post_id = _post_id_from(final)
    if not post_id:
        raise NotARedditPost(
            f"Could not find a post id in {url} (redirected to {final}). "
            "Subreddit, user and comment-search links are not supported."
        )
    return ResolvedLink(post_id, _strip(final), source)
