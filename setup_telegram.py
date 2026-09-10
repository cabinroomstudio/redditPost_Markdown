"""Find your Telegram chat id without starting the bot.

Put TELEGRAM_BOT_TOKEN in .env, message your bot anything, then run:
    python setup_telegram.py

It reads pending updates without consuming them, so the bot still sees them later.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

from config import Config


def call(token: str, method: str, params: dict | None = None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        print(f"HTTP {exc.code} from {method}: {body}")
        if exc.code == 401:
            print("\nThat token was rejected. Copy it again from @BotFather - it looks like\n"
                  "  1234567890:AAExampleTokenStringFromBotFather\n"
                  "and goes in .env as TELEGRAM_BOT_TOKEN=... with no quotes.")
        sys.exit(1)


def main() -> int:
    cfg = Config.load()
    token = cfg.telegram.token
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set.\n"
              "Copy .env.example to .env and paste the token @BotFather gave you.")
        return 1

    me = call(token, "getMe")
    if not me.get("ok"):
        print(f"getMe failed: {me}")
        return 1
    bot = me["result"]
    print(f"Bot: @{bot.get('username')} ({bot.get('first_name')})\n")

    result = call(token, "getUpdates", {"timeout": 0, "limit": 20})
    updates = result.get("result", [])
    if not updates:
        print("No messages waiting.\n"
              "Open Telegram, find your bot, send it any message, then run this again.\n"
              "(Telegram holds undelivered messages for 24 hours.)")
        return 1

    seen: dict[str, str] = {}
    for update in updates:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if chat.get("id") is None:
            continue
        who = chat.get("username") or chat.get("first_name") or chat.get("title") or "?"
        seen[str(chat["id"])] = f"{who} ({chat.get('type')})"

    print("Chat ids found:")
    for chat_id, who in seen.items():
        print(f"  {chat_id}   {who}")

    if len(seen) == 1:
        only = next(iter(seen))
        print(f"\nAdd this line to .env:\n\n    TELEGRAM_ALLOWED_CHAT_ID={only}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
