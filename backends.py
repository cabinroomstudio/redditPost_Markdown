"""Backend selection: which source a thread is fetched from, and what happens
when that source fails.

- "arctic"   Arctic Shift community archive - no credentials, works out of the box
- "official" Reddit's own API - needs a script app, always current, but Reddit
             blocks unauthenticated reads so it is useless without credentials

"auto" prefers the official API when credentials exist (it is authoritative and
never lags), otherwise Arctic Shift, and falls back to the other on failure so a
single service being down does not lose a capture.
"""
from __future__ import annotations

import backend_arctic
import fetch
from config import Config
from fetch import Thread
from http_util import RedditHttp


def backend_order(cfg: Config) -> list[str]:
    choice = (cfg.reddit.backend or "auto").lower()
    if choice == "arctic":
        return ["arctic"]
    if choice == "official":
        return ["official"]
    if cfg.reddit.use_oauth:
        return ["official", "arctic"]
    return ["arctic"]


def describe(cfg: Config) -> str:
    order = backend_order(cfg)
    primary = order[0]
    if primary == "official":
        return f"Reddit API ({cfg.reddit.grant_type})" + (
            ", Arctic Shift as fallback" if len(order) > 1 else "")
    return "Arctic Shift (no credentials needed)"


def get_thread(post_id: str, http: RedditHttp, cfg: Config, log=print) -> Thread:
    errors: list[str] = []
    for name in backend_order(cfg):
        try:
            if name == "official":
                return fetch.fetch_thread(post_id, http, cfg.reddit, log=log)
            return backend_arctic.fetch_thread(post_id, http, cfg.reddit, cfg.arctic, log=log)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            log(f"  backend '{name}' failed ({exc})")
    raise RuntimeError("All backends failed - " + " | ".join(errors))
