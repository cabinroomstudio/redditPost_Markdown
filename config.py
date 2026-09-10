"""Configuration: dataclass defaults, overridden by config.toml, secrets from .env."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_env(path: Path | None = None) -> None:
    """Minimal KEY=VALUE .env reader. Existing environment wins."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Paths:
    output_dir: Path = Path.home() / "Documents" / "reddit-notes"
    versions_dirname: str = "_versions"
    state_dir: Path = ROOT / "state"

    @property
    def versions_dir(self) -> Path:
        return self.output_dir / self.versions_dirname

    @property
    def db_path(self) -> Path:
        return self.state_dir / "reddit-md.db"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "bot.log"


@dataclass
class ArcticCfg:
    """Arctic Shift: community archive of Reddit data, no credentials needed.

    Returns the same JSON shape as Reddit's own API, so the parser is shared.
    """
    base: str = "https://arctic-shift.photon-reddit.com/api"
    comment_limit: int = 5000
    timeout: int = 90


@dataclass
class RedditCfg:
    # auto = official API when credentials exist, else Arctic Shift; the other
    # one is tried as a fallback. Force with "arctic" or "official".
    backend: str = "auto"
    username: str = ""          # only used to build a contactable User-Agent
    request_delay: float = 1.2  # seconds between requests
    retries: int = 3
    comment_limit: int = 500
    sort: str = "top"
    max_more_calls: int = 40     # `more` batches per thread
    max_deep_fetches: int = 10   # "continue this thread" sub-fetches per thread
    timeout: int = 30
    # Populated from .env. Reddit blocks anonymous .json reads, so these are
    # required in practice; username/password stay optional.
    client_id: str = ""
    client_secret: str = ""
    auth_username: str = ""
    auth_password: str = ""

    @property
    def use_oauth(self) -> bool:
        """OAuth needs only an app id + secret."""
        return bool(self.client_id and self.client_secret)

    @property
    def grant_type(self) -> str:
        """client_credentials is app-only and read-only: no account password on disk."""
        if self.auth_username and self.auth_password:
            return "password"
        return "client_credentials"

    @property
    def user_agent(self) -> str:
        who = self.username or self.auth_username or "anonymous"
        return f"windows:reddit-md:v1.0 (by /u/{who})"


@dataclass
class FilterCfg:
    min_score: int = 1
    short_len: int = 15
    short_min_score: int = 5
    drop_stickied_mod: bool = True  # pinned mod/vote-bot boilerplate
    bot_authors: list[str] = field(default_factory=lambda: [
        "AutoModerator", "RemindMeBot", "sneakpeekbot", "B0tRank",
        "qualityvote", "qualityvote2", "SubredditLinkBot",
        "WikiSummarizerBot", "AmputatorBot", "SaveVideo", "savevideobot",
        "VredditDownloader", "stabbot", "GoodBot_BadBot", "totesmessenger",
        "PoliticsModeratorBot", "timee_bot", "converter-bot", "haikusbot",
    ])
    bot_suffixes: list[str] = field(default_factory=lambda: ["-bot", "_bot", "Bot"])


@dataclass
class VersionCfg:
    min_hours_between: int = 12
    max_versions: int = 6
    watch_days: int = 14
    # value gate thresholds (see README table)
    rule_a_new_words: int = 400
    rule_a_ratio: float = 0.20
    rule_b_min_score: int = 25
    rule_b_ratio: float = 0.40
    rule_c_new_comments: int = 15
    rule_c_new_words: int = 200
    rule_d_score_multiple: float = 3.0
    rule_d_new_comments: int = 5
    # rule e: relative growth. Small captures in low-score subreddits never trip
    # the absolute floors above - scores stay at 1 and totals stay small - but a
    # thread that doubles its substance is worth a version whatever its size.
    rule_e_growth_multiple: float = 2.0
    rule_e_new_words: int = 150
    rule_e_new_comments: int = 4
    # hours after first capture at which re-checks happen
    backoff_hours: list[int] = field(default_factory=lambda: [6, 24, 72, 168, 336])
    retire_after_no_change: int = 2


@dataclass
class TelegramCfg:
    token: str = ""
    allowed_chat_id: str = ""
    poll_timeout: int = 50
    notify_on_version: bool = True
    max_checks_per_tick: int = 5
    # Warn when the bot has been down long enough that Telegram may have dropped
    # queued shares (its queue holds undelivered updates for 24h).
    offline_warn_hours: int = 20


@dataclass
class Config:
    paths: Paths = field(default_factory=Paths)
    reddit: RedditCfg = field(default_factory=RedditCfg)
    arctic: ArcticCfg = field(default_factory=ArcticCfg)
    filters: FilterCfg = field(default_factory=FilterCfg)
    versioning: VersionCfg = field(default_factory=VersionCfg)
    telegram: TelegramCfg = field(default_factory=TelegramCfg)

    @classmethod
    def load(cls, toml_path: Path | None = None) -> "Config":
        load_env()
        cfg = cls()
        path = toml_path or ROOT / "config.toml"
        if path.exists():
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            for section in ("paths", "reddit", "arctic", "filters", "versioning", "telegram"):
                if section in data:
                    _apply(getattr(cfg, section), data[section])

        env = os.environ.get
        cfg.telegram.token = env("TELEGRAM_BOT_TOKEN", cfg.telegram.token)
        cfg.telegram.allowed_chat_id = env("TELEGRAM_ALLOWED_CHAT_ID", cfg.telegram.allowed_chat_id)
        cfg.reddit.client_id = env("REDDIT_CLIENT_ID", cfg.reddit.client_id)
        cfg.reddit.client_secret = env("REDDIT_CLIENT_SECRET", cfg.reddit.client_secret)
        cfg.reddit.auth_username = env("REDDIT_USERNAME", cfg.reddit.auth_username)
        cfg.reddit.auth_password = env("REDDIT_PASSWORD", cfg.reddit.auth_password)
        return cfg


def _apply(obj, table: dict) -> None:
    """Copy known keys from a toml table onto a dataclass, coercing Path fields."""
    for key, value in table.items():
        if not hasattr(obj, key):
            continue
        current = getattr(obj, key)
        if isinstance(current, Path):
            value = Path(value).expanduser()
        setattr(obj, key, value)
