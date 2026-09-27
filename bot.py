"""The daemon: Telegram long-poll plus the re-check scheduler, one process.

Started at logon by Task Scheduler. If the laptop is off, Telegram queues
messages for 24h and they drain on the next start; scheduled re-checks simply
run late, which is harmless.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
import time
from datetime import timedelta
from pathlib import Path

from config import Config
from http_util import RedditBlocked, RedditHttp, is_transient
from pipeline import process_url, recheck_due
from resolve import NotARedditPost, extract_urls
from store import (all_pending, connect, drop_pending, due_pending, get_meta, get_post,
                   iso, now_utc, parse, queue_pending, reschedule_pending, retire,
                   set_meta, watched_posts)
from telegram import Telegram, TelegramError

HELP = (
    "Send me a Reddit link and I'll save the thread as markdown.\n\n"
    "/status — what I'm watching and when I check next\n"
    "/force <url> — re-check now, ignoring the 12h gate\n"
    "/stop <url> — stop watching a post\n"
    "/help — this message"
)


def setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("reddit-md")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)
    return logger


def handle_message(message, cfg: Config, conn, http: RedditHttp, tg: Telegram, log) -> None:
    text = message.text.strip()
    reply = lambda body: tg.send(message.chat_id, body, message.message_id)

    if text.startswith("/start") or text.startswith("/help"):
        reply(HELP)
        return

    if text.startswith("/status"):
        rows = watched_posts(conn)
        queued = all_pending(conn)
        if not rows and not queued:
            reply("Nothing under watch right now.")
            return
        lines = []
        if rows:
            lines.append("Watching:")
            for row in rows[:20]:
                when = parse(row["next_check_at"])
                when_str = when.astimezone().strftime("%d %b %H:%M") if when else "—"
                lines.append(f"• v{row['current_version']} · {row['title'][:50]} → {when_str}")
        if queued:
            lines.append("\nWaiting to retry (source was down):")
            for row in queued[:20]:
                when = parse(row["next_try"]).astimezone().strftime("%d %b %H:%M")
                lines.append(f"• {row['url'][:60]} · attempt {row['attempts']} → {when}")
        reply("\n".join(lines))
        return

    force = text.startswith("/force")
    if text.startswith("/stop"):
        urls = extract_urls(text)
        if not urls:
            reply("Send /stop with the post URL.")
            return
        try:
            from resolve import resolve
            link = resolve(urls[0], http)
        except (NotARedditPost, RedditBlocked) as exc:
            reply(f"⚠️ {exc}")
            return
        if get_post(conn, link.post_id):
            retire(conn, link.post_id)
            reply("Stopped watching that post. The markdown file stays where it is.")
        else:
            reply("I'm not watching that post.")
        return

    urls = [u for u in extract_urls(text)]
    if not urls:
        reply("That didn't contain a link. " + HELP)
        return

    for url in urls:
        try:
            outcome = process_url(url, cfg, conn, http, force=force, log=log.info)
            reply(outcome.message)
            log.info(f"{outcome.kind}: {url} -> {outcome.path}")
        except NotARedditPost as exc:
            reply(f"⚠️ {exc}")
        except RedditBlocked as exc:
            reply(f"🚫 Reddit refused the request.\n{exc}")
            log.error(f"blocked on {url}: {exc}")
        except Exception as exc:
            if is_transient(exc):
                when = now_utc() + timedelta(minutes=_backoff(cfg, 1))
                queue_pending(conn, url, message.chat_id, message.message_id, force,
                              when, str(exc))
                reply(f"⏳ Couldn't fetch that thread right now - the source looks "
                      f"temporarily down.\n{_short(exc)}\n\nQueued: I'll keep retrying "
                      f"(next at {when.astimezone():%H:%M}) and reply here once it's saved.")
                log.warning(f"queued {url} for retry: {exc}")
            else:
                reply(f"❌ Failed on that link: {exc}")
                log.exception(f"unhandled error on {url}")


def _backoff(cfg: Config, attempt: int) -> int:
    """Minutes to wait after the given attempt; the last step repeats."""
    steps = cfg.retry.backoff_minutes or [60]
    return steps[min(attempt, len(steps)) - 1]


def _short(exc: Exception, limit: int = 300) -> str:
    text = str(exc)
    return text if len(text) <= limit else text[:limit] + "…"


def retry_pending(cfg: Config, conn, http: RedditHttp, tg: Telegram, log) -> None:
    """Re-attempt shares that failed because a source was down."""
    for row in due_pending(conn, cfg.telegram.max_checks_per_tick):
        url, chat_id, reply_to = row["url"], row["chat_id"], row["message_id"]
        attempt = row["attempts"] + 1
        log.info(f"retrying queued {url} (attempt {attempt})")
        try:
            outcome = process_url(url, cfg, conn, http, force=bool(row["force"]),
                                  log=log.info)
        except Exception as exc:
            first = parse(row["first_tried"]) or now_utc()
            gave_up = now_utc() - first >= timedelta(hours=cfg.retry.give_up_hours)
            if is_transient(exc) and not gave_up:
                when = now_utc() + timedelta(minutes=_backoff(cfg, attempt))
                reschedule_pending(conn, url, when, str(exc))
                log.warning(f"retry {attempt} failed for {url}: {exc}")
                continue
            drop_pending(conn, url)
            reason = (f"still failing after {cfg.retry.give_up_hours}h of retries"
                      if gave_up else "a permanent error")
            tg.send(chat_id, f"❌ Gave up on this link ({reason}):\n{_short(exc)}\n\n"
                             "Send it again later to retry.", reply_to)
            log.error(f"gave up on queued {url}: {exc}")
            continue
        drop_pending(conn, url)
        tg.send(chat_id, f"{outcome.message}\n(saved on retry {attempt})", reply_to)
        log.info(f"{outcome.kind} on retry: {url} -> {outcome.path}")


def warn_offline_gap(conn, tg: Telegram, cfg: Config, allowed: str, log) -> None:
    """Tell the user when queued shares may have been dropped.

    Telegram holds undelivered bot updates for 24h, and the Bot API cannot read
    past chat messages - so a share made while this machine was off for longer
    than that is simply gone, with no error anywhere. The least we can do is turn
    that silent loss into a visible prompt to re-send.
    """
    last_seen = parse(get_meta(conn, "last_seen", ""))
    if last_seen is None:
        return  # first run: nothing was missed
    gap_hours = (now_utc() - last_seen).total_seconds() / 3600
    if gap_hours < cfg.telegram.offline_warn_hours:
        return
    log.warning(f"offline for {gap_hours:.1f}h - queued shares may have expired")
    if not allowed:
        return
    since = last_seen.astimezone().strftime("%d %b %H:%M")
    until = now_utc().astimezone().strftime("%d %b %H:%M")
    tg.send(allowed,
            f"⚠️ I was offline from {since} to {until} ({gap_hours:.0f}h).\n\n"
            "Telegram only holds undelivered messages for 24h, so anything you shared "
            "in that window may have been dropped. Scroll up: any link without a reply "
            "from me was not saved - forward it again and I'll pick it up.")


def main() -> int:
    cfg = Config.load()
    log = setup_logging(cfg.paths.log_path)
    log.info("starting reddit-md bot")

    from backends import describe
    log.info(f"backend: {describe(cfg)}")
    try:
        tg = Telegram(cfg.telegram, log=log.warning)
    except TelegramError as exc:
        log.error(str(exc))
        return 2

    conn = connect(cfg.paths.db_path)
    http = RedditHttp(cfg.reddit, log=log.info)
    offset = int(get_meta(conn, "telegram_offset", "0") or 0)
    allowed = str(cfg.telegram.allowed_chat_id or "")

    warn_offline_gap(conn, tg, cfg, allowed, log)

    while True:
        try:
            messages, offset = tg.poll(offset)
            set_meta(conn, "telegram_offset", str(offset))
            set_meta(conn, "last_seen", iso(now_utc()))
            for message in messages:
                if allowed and message.chat_id != allowed:
                    log.warning(f"ignoring message from chat {message.chat_id} ({message.sender})")
                    continue
                if not allowed:
                    tg.send(message.chat_id,
                            f"This chat's id is {message.chat_id}. Put it in .env as "
                            f"TELEGRAM_ALLOWED_CHAT_ID and restart me, so only you can use this bot.")
                    log.warning(f"no allowlist set; chat id is {message.chat_id}")
                    continue
                handle_message(message, cfg, conn, http, tg, log)

            retry_pending(cfg, conn, http, tg, log)

            for outcome in recheck_due(cfg, conn, http, log=log.info):
                if outcome.kind == "updated" and cfg.telegram.notify_on_version and allowed:
                    tg.send(allowed, outcome.message)
                log.info(f"recheck {outcome.kind}: {outcome.path}")

        except TelegramError as exc:
            log.error(f"telegram error: {exc}")
            time.sleep(30)
        except KeyboardInterrupt:
            log.info("stopping")
            return 0
        except Exception:
            log.exception("unexpected error in main loop")
            time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
