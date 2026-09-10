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
from pathlib import Path

from config import Config
from http_util import RedditBlocked, RedditHttp
from pipeline import process_url, recheck_due
from resolve import NotARedditPost, extract_urls
from store import connect, get_meta, get_post, parse, retire, set_meta, watched_posts
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
        if not rows:
            reply("Nothing under watch right now.")
            return
        lines = ["Watching:"]
        for row in rows[:20]:
            when = parse(row["next_check_at"])
            when_str = when.astimezone().strftime("%d %b %H:%M") if when else "—"
            lines.append(f"• v{row['current_version']} · {row['title'][:50]} → {when_str}")
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
            reply(f"❌ Failed on that link: {exc}")
            log.exception(f"unhandled error on {url}")


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

    while True:
        try:
            messages, offset = tg.poll(offset)
            set_meta(conn, "telegram_offset", str(offset))
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
