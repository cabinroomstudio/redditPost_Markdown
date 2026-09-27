"""Backend selection: which source a thread is fetched from, and what happens
when that source fails.

- "arctic"   Arctic Shift community archive - no credentials, works out of the box
- "pullpush" PullPush community archive - no credentials; fallback for when Arctic
             Shift is down (flat data, scores may be stale)
- "official" Reddit's own API - needs a script app, always current, but Reddit
             blocks unauthenticated reads so it is useless without credentials

"auto" prefers the official API when credentials exist (it is authoritative and
never lags), otherwise Arctic Shift, and falls back through the rest in order so
a single service being down does not lose a capture. If every source fails,
SourcesUnavailable says whether the failure looks temporary, so the caller can
queue the link for a retry instead of giving up.
"""
from __future__ import annotations

import backend_arctic
import backend_pullpush
import fetch
from config import Config
from fetch import Thread
from http_util import RedditHttp, is_transient

ARCHIVES = ["arctic", "pullpush"]


class SourcesUnavailable(RuntimeError):
    """Every backend failed. `transient` is true when retrying later may work."""

    def __init__(self, errors: list[tuple[str, Exception]]):
        self.errors = errors
        self.transient = any(is_transient(exc) for _, exc in errors)
        detail = " | ".join(f"{name}: {exc}" for name, exc in errors)
        super().__init__(f"No source could serve this thread - {detail}")


def backend_order(cfg: Config) -> list[str]:
    choice = (cfg.reddit.backend or "auto").lower()
    if choice in ("arctic", "pullpush", "official"):
        return [choice]
    if cfg.reddit.use_oauth:
        return ["official", *ARCHIVES]
    return list(ARCHIVES)


def describe(cfg: Config) -> str:
    order = backend_order(cfg)
    primary = order[0]
    if primary == "official":
        return f"Reddit API ({cfg.reddit.grant_type})" + (
            ", archives as fallback" if len(order) > 1 else "")
    return " -> ".join(order) + " (no credentials needed)"


def get_thread(post_id: str, http: RedditHttp, cfg: Config, log=print) -> Thread:
    errors: list[tuple[str, Exception]] = []
    for name in backend_order(cfg):
        try:
            if name == "official":
                return fetch.fetch_thread(post_id, http, cfg.reddit, log=log)
            if name == "pullpush":
                return backend_pullpush.fetch_thread(
                    post_id, http, cfg.reddit, cfg.pullpush, log=log)
            return backend_arctic.fetch_thread(post_id, http, cfg.reddit, cfg.arctic, log=log)
        except Exception as exc:
            errors.append((name, exc))
            log(f"  backend '{name}' failed ({exc})")
    raise SourcesUnavailable(errors)
