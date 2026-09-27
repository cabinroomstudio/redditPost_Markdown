# reddit-md

Share a Reddit link to a Telegram bot; get a markdown file of the whole thread in
`<home>\Documents\reddit-notes`. The post stays under watch for two weeks
and a new version is written **only** when materially more useful content shows up.

**Requirements:** Python 3.11+ (uses `tomllib`). No third-party packages — the whole
thing runs on the standard library.

## Setup (two manual steps, ~5 minutes)

### 1. Telegram bot

1. Open Telegram, message **@BotFather**, send `/newbot`.
2. Give it any name and a username ending in `bot`.
3. Copy the token it returns into `.env` as `TELEGRAM_BOT_TOKEN`.
4. Open a chat with your new bot and send it anything.
5. Run `python setup_telegram.py` — it prints the chat id of whoever messaged the bot.
   Put that in `.env` as `TELEGRAM_ALLOWED_CHAT_ID`. That allowlist is what stops
   anyone who guesses the token from using your bot.

   (Once the bot is running it also answers with the chat id itself, but it has to be
   running to do that — hence the helper for first-time setup.)

### 2. Reddit access — nothing to do

Reddit blocks unauthenticated JSON reads (`www.reddit.com/*.json` → 403 Blocked,
`old.reddit.com` → login wall, as of September 2026), so threads are
fetched from **Arctic Shift** instead — a community archive that serves Reddit's
own JSON shape and needs no credentials. It returns comment ids, scores, bodies,
authors and full nesting, which is everything the versioning logic depends on.

**Optional upgrade.** Reddit's official API is authoritative and never lags the
live site. To use it, register a **script** app at
<https://www.reddit.com/prefs/apps> (the button is at the bottom of that page),
put `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` in `.env`, and your Reddit
username in `config.toml` under `[reddit] username`. No Reddit password is needed:
with only an id and secret the client uses the app-only `client_credentials`
grant. Once those exist, the official API becomes primary automatically and
the archives become the fallback. Force one with `[reddit] backend`.

**Fallback archive.** If Arctic Shift is down, threads are fetched from
[PullPush](https://pullpush.io) instead (also no credentials). Its data is flat, so
the tree is rebuilt from parent ids, and scores are as of its last scrape.

Then verify:

```
python cli.py --auth-test
```

### 3. Run at logon

```
powershell -ExecutionPolicy Bypass -File install_task.ps1
Start-ScheduledTask -TaskName reddit-md
```

## Daily use

Phone: Reddit → Share → Telegram → your bot chat. That's it.

Bot commands: `/status`, `/force <url>`, `/stop <url>`, `/help`.

## Command line

```
python cli.py <url>            capture or re-check, write the file
python cli.py <url> --stdout   print the markdown, write nothing
python cli.py <url> --force    re-check now, ignore the 12h gate
python cli.py --check-due      run one scheduler pass
python cli.py --status         list watched posts
python tests.py                fixture suite (no network needed)
```

## How versioning decides

Every capture records its exact set of kept comment ids, so "what is new" is a set
difference, not a guess. A re-check writes a new version only if **all** hard gates
pass — 12h since the last version, under 6 versions, still inside the 14-day window
— and **any** of these value rules fires:

| Rule | Fires when |
|---|---|
| a | +400 words **and** +20% over the previous version's word count |
| b | A new comment scoring ≥ max(25, 40% of the old top comment) |
| c | +15 comments **and** +200 words |
| d | Post score tripled **and** +5 comments — the "caught it early, then it took off" case |
| e | The thread at least **doubled** its substance (+150 words, +4 comments) — catches small threads in niche subreddits, where every comment sits at 1 point and the absolute floors above are never reached |

Otherwise the check is silent: metrics are recorded, no file is touched, no message
is sent. Re-checks follow a decaying ladder from first capture: **+6h, +24h, +3d,
+7d, +14d**, then the post retires. Two consecutive checks with nothing new retire
it sooner. All thresholds are in `config.toml`.

When a version is promoted: the current file is copied to
`reddit-notes\_versions\<name>.v<N>.md`, the live file is rewritten with the full
current thread, and a `## Changelog` line records what changed.

## Filtering

"Light + collapse chatter" — everything at every depth is kept except: deleted and
removed bodies, AutoModerator and known bots, comments scoring below 1, and
comments under 15 characters scoring below 5. A dropped comment that still has
surviving replies is kept as a stub so answer chains never lose their question.

## Layout

| File | Role |
|---|---|
| `bot.py` | daemon: Telegram long-poll + scheduler tick |
| `telegram.py` | the only file that knows about Telegram (intake is swappable) |
| `resolve.py` | share links (`/s/…`, `redd.it`) → canonical post id |
| `backends.py` | picks the source and falls back when one is down |
| `backend_arctic.py` | Arctic Shift fetcher (default, no credentials) |
| `backend_pullpush.py` | PullPush fetcher (fallback archive, no credentials) |
| `fetch.py` | Reddit API fetcher + `more` expansion (pooled, 100 ids per call) |
| `filters.py` | keep/drop rules |
| `render.py` | thread → markdown |
| `versioning.py` | metrics and the promotion gates |
| `pipeline.py` | fetch → filter → decide → write |
| `store.py` | SQLite state |
| `tests.py` | fixture suite |

## Known limits

- **Telegram queues undelivered messages for 24 hours.** Laptop off overnight is
  fine. Off for three days means those shares are dropped from the queue — and the
  Bot API cannot read past chat messages, so no backfill is possible. The messages
  themselves stay in the chat, so forwarding one again captures it normally. On
  startup the bot detects a gap longer than `offline_warn_hours` (default 20) and
  messages you to re-send anything from that window, so the loss is visible rather
  than silent. Intake is isolated in `telegram.py`, so an email-to-self intake
  (no expiry) can be added later without touching the engine.
- The scheduled task triggers **at logon**, not at boot: powering the machine on
  without signing in leaves the bot down.
- Scheduled re-checks are driven by the database, not the queue, so they are never
  lost — they simply run late after the machine comes back.
- Very large threads hit a cap (Arctic Shift: 5,000 comments; the Reddit API: 40
  `more` batches ≈ 4,000). When that happens the file says so above the comments.
- **The archives are community-run services.** If Arctic Shift is down, PullPush is
  tried next. If every source fails with a temporary error (5xx, timeout, network
  down, post not indexed yet), the share is **queued, not dropped**: the bot replies
  ⏳, retries on a backoff (`[retry] backoff_minutes`), replies with the saved note
  when it succeeds, and gives up with a message after `give_up_hours` (48).
  `/status` lists the queue. Scheduled re-checks hit by an outage are retried an hour
  later without losing their place on the ladder. Registering the Reddit app (above)
  adds the most reliable source of all.
- The archives index with a delay, so a post shared within minutes of being created
  may not be found yet. That counts as temporary: it is queued and retried.
- A moderator-removed post reports its title as `[ Removed by moderator ]`. The
  original is recovered from the URL slug and the note is flagged
  `removed_by_moderator: true` — the archived comments are still saved.
- Private, quarantined and 18+ subreddits need a logged-in account: fill in
  `REDDIT_USERNAME` / `REDDIT_PASSWORD` to switch to the password grant.

## Credits

Thread data comes from [Arctic Shift](https://github.com/ArthurHeitmann/arctic_shift),
a community-run archive of Reddit data. It is a free service run by volunteers —
`request_delay` in `config.toml` (1.2s between calls) is there to keep this tool a
polite client, and please leave it that way. If you plan to capture at volume, use
the official Reddit API path instead.

## License

MIT — see [LICENSE](LICENSE).
