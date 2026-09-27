"""Command line entry point - same engine as the bot, useful for testing.

  python cli.py <url>              capture (or re-check) a thread and write the file
  python cli.py <url> --stdout     print the markdown, touch nothing
  python cli.py <url> --force      re-check now, ignoring the 12h gate
  python cli.py --check-due        run the scheduler pass once
  python cli.py --status           list watched posts
  python cli.py --auth-test        confirm Reddit credentials work
"""
from __future__ import annotations

import argparse
import sys

from config import Config
from backends import describe, get_thread
from filters import filter_thread, kept_comments
from http_util import RedditBlocked, RedditHttp
from pipeline import process_url, recheck_due
from render import render
from resolve import NotARedditPost, resolve
from store import connect, now_utc, parse, watched_posts


def main() -> int:
    parser = argparse.ArgumentParser(description="Reddit thread -> markdown")
    parser.add_argument("url", nargs="?", help="Reddit post URL (share links are resolved)")
    parser.add_argument("--stdout", action="store_true", help="print markdown, write nothing")
    parser.add_argument("--force", action="store_true", help="ignore the 12h version gate")
    parser.add_argument("--check-due", action="store_true", help="run one scheduler pass")
    parser.add_argument("--status", action="store_true", help="list watched posts")
    parser.add_argument("--auth-test", action="store_true", help="verify Reddit access")
    args = parser.parse_args()

    cfg = Config.load()
    http = RedditHttp(cfg.reddit, log=print)

    if args.auth_test:
        print(f"Backend in use : {describe(cfg)}")
        print(f"User-Agent     : {cfg.reddit.user_agent}\n")
        ok = False

        print("Arctic Shift (no credentials):")
        try:
            payload = http.json_get(
                f"{cfg.arctic.base}/posts/ids?ids=1w6t678", timeout=cfg.arctic.timeout)
            data = payload.get("data") or []
            post = data[0] if data else {}
            print(f"  OK - {post.get('num_comments')} comments, "
                  f"score {post.get('score')}, r/{post.get('subreddit')}")
            ok = True
        except Exception as exc:
            print(f"  FAILED - {exc}")

        print("\nPullPush (fallback, no credentials):")
        try:
            payload = http.json_get(
                f"{cfg.pullpush.base}/reddit/search/submission/?ids=1w6t678",
                timeout=cfg.pullpush.timeout)
            data = payload.get("data") or []
            post = data[0] if data else {}
            print(f"  OK - {post.get('num_comments')} comments, r/{post.get('subreddit')}")
            ok = True
        except Exception as exc:
            print(f"  FAILED - {exc}")

        print("\nReddit official API:")
        if not cfg.reddit.use_oauth:
            print("  skipped - REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set in .env")
            print("  (optional: Arctic Shift covers everything without them)")
        else:
            try:
                data = http.api_get("/r/ChatGPTPro/top", {"t": "week", "limit": 3})
                posts = data["data"]["children"]
                print(f"  OK - fetched {len(posts)} posts via {cfg.reddit.grant_type}")
                ok = True
            except RedditBlocked as exc:
                print(f"  BLOCKED - {exc}")
            except Exception as exc:
                print(f"  FAILED - {exc}")
        return 0 if ok else 1

    if args.status:
        conn = connect(cfg.paths.db_path)
        rows = watched_posts(conn)
        if not rows:
            print("Nothing under watch.")
            return 0
        for row in rows:
            when = parse(row["next_check_at"])
            when_str = when.astimezone().strftime("%Y-%m-%d %H:%M") if when else "-"
            print(f"v{row['current_version']}  next {when_str}  {row['title'][:60]}")
        return 0

    if args.check_due:
        conn = connect(cfg.paths.db_path)
        outcomes = recheck_due(cfg, conn, http, log=print)
        if not outcomes:
            print("Nothing due.")
        for outcome in outcomes:
            print(f"[{outcome.kind}] {outcome.message}")
        return 0

    if not args.url:
        parser.print_help()
        return 1

    try:
        if args.stdout:
            link = resolve(args.url, http)
            thread = get_thread(link.post_id, http, cfg, log=lambda m: print(m, file=sys.stderr))
            filter_thread(thread, cfg.filters)
            print(f"[{len(kept_comments(thread))} comments kept of {thread.num_comments} reported]",
                  file=sys.stderr)
            sys.stdout.reconfigure(encoding="utf-8")
            print(render(thread, version=0, captured_at=now_utc(), changelog=[]))
            return 0

        conn = connect(cfg.paths.db_path)
        outcome = process_url(args.url, cfg, conn, http, force=args.force, log=print)
        print(f"\n[{outcome.kind}] {outcome.message}")
        return 0
    except NotARedditPost as exc:
        print(f"Not a Reddit post: {exc}")
        return 1
    except RedditBlocked as exc:
        print(f"Reddit blocked the request:\n{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
