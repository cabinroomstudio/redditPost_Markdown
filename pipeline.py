"""Orchestration shared by the CLI and the bot: fetch -> filter -> decide -> write."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from config import Config
from backends import get_thread
from fetch import Thread
from filters import filter_thread
from http_util import RedditHttp, is_transient
from render import changelog_line, filename_for, render
from resolve import resolve
from store import (add_version, all_versions, create_post, get_post, iso, latest_version,
                   now_utc, parse, postpone, retire, update_after_check)
from versioning import decide, measure, next_check


@dataclass
class Outcome:
    kind: str            # created | updated | unchanged | error
    message: str         # ready to send to Telegram
    path: Path | None = None
    version: int = 0


def _archive(path: Path, versions_dir: Path, previous_version: int) -> None:
    if not path.exists():
        return
    versions_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, versions_dir / f"{path.stem}.v{previous_version}{path.suffix}")


def _target_path(cfg: Config, thread: Thread, stored: str | None) -> Path:
    """Reuse the stored path when it still exists, so manual renames survive."""
    if stored:
        candidate = Path(stored)
        if candidate.exists():
            return candidate
    return cfg.paths.output_dir / filename_for(thread)


def _write(cfg: Config, conn, thread: Thread, metrics, version: int, note: str,
           stored_path: str | None) -> Path:
    path = _target_path(cfg, thread, stored_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if version > 1:
        _archive(path, cfg.paths.versions_dir, version - 1)

    lines = [changelog_line(row["version"], parse(row["created_at"]),
                            row["kept_comments"], row["kept_words"], row["note"] or "")
             for row in all_versions(conn, thread.post_id)]
    lines.append(changelog_line(version, now_utc(), metrics.kept_comments,
                                metrics.kept_words, note))

    body = render(thread, version=version, captured_at=now_utc(), changelog=lines)
    path.write_text(body, encoding="utf-8")
    return path


def _schedule(cfg: Config, conn, post_row, changed: bool, version: int,
              path: Path | None, title: str) -> datetime | None:
    """Advance the decaying ladder and retire posts that are done."""
    vcfg = cfg.versioning
    first = parse(post_row["first_captured"]) or now_utc()
    watch_until = parse(post_row["watch_until"])
    index = post_row["check_index"]
    no_change = 0 if changed else post_row["consecutive_no_change"] + 1

    when = next_check(index, first, vcfg)
    while when is not None and when <= now_utc():
        index += 1
        when = next_check(index, first, vcfg)

    retired = (
        when is None
        or (watch_until is not None and when > watch_until)
        or no_change >= vcfg.retire_after_no_change
    )
    update_after_check(
        conn, post_row["post_id"],
        next_check_at=None if retired else when,
        check_index=index + 1,
        consecutive_no_change=no_change,
        retired=retired,
        version=version,
        file_path=str(path) if path else None,
        title=title,
    )
    return None if retired else when


def _when_str(when: datetime | None) -> str:
    if when is None:
        return "no further checks (retired)"
    delta = when - now_utc()
    hours = max(delta.total_seconds() / 3600, 0)
    if hours < 48:
        return f"next check in {hours:.0f}h"
    return f"next check in {hours / 24:.0f}d"


def process(post_id: str, url: str, cfg: Config, conn, http: RedditHttp, *,
            force: bool = False, log=print) -> Outcome:
    thread = get_thread(post_id, http, cfg, log=log)
    filter_thread(thread, cfg.filters)
    metrics = measure(thread)

    post_row = get_post(conn, post_id)
    previous = latest_version(conn, post_id) if post_row else None
    version_count = len(all_versions(conn, post_id)) if post_row else 0

    verdict = decide(previous, metrics, cfg.versioning,
                     version_count=version_count, now=now_utc(), forced=force)

    # ---- first capture ----
    if post_row is None:
        path = _write(cfg, conn, thread, metrics, 1, "first capture", None)
        add_version(conn, post_id, 1, metrics, "first capture")
        create_post(conn, post_id=post_id, url=thread.permalink or url,
                    title=thread.title, subreddit=thread.subreddit,
                    file_path=str(path), watch_days=cfg.versioning.watch_days,
                    first_check_in_hours=cfg.versioning.backoff_hours[0])
        return Outcome(
            "created",
            f'✅ v1 — "{thread.title}"\n'
            f"r/{thread.subreddit} · {metrics.kept_comments:,} comments · "
            f"{metrics.kept_words:,} words\n{path.name}",
            path, 1)

    # ---- re-check ----
    if verdict.promote:
        version = post_row["current_version"] + 1
        path = _write(cfg, conn, thread, metrics, version, verdict.note,
                      post_row["file_path"])
        add_version(conn, post_id, version, metrics, verdict.note)
        when = _schedule(cfg, conn, post_row, True, version, path, thread.title)
        return Outcome(
            "updated",
            f'📈 v{version} — "{thread.title}"\n{verdict.note}\n'
            f"{metrics.kept_comments:,} comments total · {_when_str(when)}",
            path, version)

    when = _schedule(cfg, conn, post_row, verdict.new_comments > 0,
                     post_row["current_version"], None, thread.title)
    return Outcome(
        "unchanged",
        f'ℹ️ Already saved as v{post_row["current_version"]} — "{thread.title}"\n'
        f"{verdict.note} · not enough for a new version ({verdict.blocked_by})\n"
        f"{_when_str(when)}",
        Path(post_row["file_path"]) if post_row["file_path"] else None,
        post_row["current_version"])


def process_url(url: str, cfg: Config, conn, http: RedditHttp, *,
                force: bool = False, log=print) -> Outcome:
    link = resolve(url, http)
    log(f"resolved {url} -> post {link.post_id}")
    return process(link.post_id, link.url, cfg, conn, http, force=force, log=log)


def recheck_due(cfg: Config, conn, http: RedditHttp, log=print) -> list[Outcome]:
    from store import due_posts
    outcomes = []
    for row in due_posts(conn, cfg.telegram.max_checks_per_tick):
        watch_until = parse(row["watch_until"])
        if watch_until is not None and watch_until < now_utc():
            retire(conn, row["post_id"])
            continue
        try:
            log(f"re-checking {row['post_id']} ({row['title']})")
            outcomes.append(process(row["post_id"], row["url"], cfg, conn, http, log=log))
        except Exception as exc:
            log(f"re-check failed for {row['post_id']}: {exc}")
            if is_transient(exc):
                # A source outage says nothing about the thread: try again soon and
                # keep this rung, so a down archive can't silently skip a check.
                postpone(conn, row["post_id"],
                         now_utc() + timedelta(minutes=cfg.retry.recheck_retry_minutes))
            else:
                # Push the next attempt out one rung rather than hammering a broken post.
                _schedule(cfg, conn, row, False, row["current_version"], None, row["title"])
    return outcomes
