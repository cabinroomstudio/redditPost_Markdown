"""Telegram intake. Deliberately the only file that knows the bot exists.

bot.py consumes `poll()` results and nothing else, so an email or clipboard
intake can be added later as a second producer without touching the engine.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from config import TelegramCfg

API = "https://api.telegram.org/bot{token}/{method}"


@dataclass
class Incoming:
    chat_id: str
    text: str
    message_id: int
    sender: str


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, cfg: TelegramCfg, log=print):
        if not cfg.token:
            raise TelegramError(
                "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather and put "
                "the token in .env")
        self.cfg = cfg
        self.log = log

    def _call(self, method: str, params: dict, timeout: int) -> dict:
        url = API.format(token=self.cfg.token, method=method)
        body = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=body)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise TelegramError(f"{method} failed: HTTP {exc.code} {detail}") from exc
        if not payload.get("ok"):
            raise TelegramError(f"{method} failed: {payload}")
        return payload["result"]

    def poll(self, offset: int) -> tuple[list[Incoming], int]:
        """Long-poll for new messages. Returns (messages, new_offset)."""
        result = self._call(
            "getUpdates",
            {"offset": offset, "timeout": self.cfg.poll_timeout,
             "allowed_updates": json.dumps(["message", "channel_post"])},
            timeout=self.cfg.poll_timeout + 15,
        )
        messages: list[Incoming] = []
        for update in result:
            offset = max(offset, update["update_id"] + 1)
            message = update.get("message") or update.get("channel_post")
            if not message:
                continue
            text = message.get("text") or message.get("caption") or ""
            # Link-preview shares sometimes carry the URL only in an entity.
            for entity in message.get("entities", []) or []:
                if entity.get("type") == "text_link" and entity.get("url"):
                    text += " " + entity["url"]
            chat = message.get("chat", {})
            sender = message.get("from", {}).get("username") or str(chat.get("id"))
            messages.append(Incoming(str(chat.get("id")), text.strip(),
                                     message.get("message_id", 0), sender))
        return messages, offset

    def send(self, chat_id: str, text: str, reply_to: int | None = None) -> None:
        params = {"chat_id": chat_id, "text": text[:4000],
                  "disable_web_page_preview": "true"}
        if reply_to:
            params["reply_to_message_id"] = reply_to
        try:
            self._call("sendMessage", params, timeout=30)
        except TelegramError as exc:
            # A failed reply must never take the daemon down.
            self.log(f"sendMessage failed: {exc}")
