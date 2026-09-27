"""HTTP layer: polite rate limiting, retries, and the anonymous -> OAuth switch.

Every Reddit request in the project goes through RedditHttp so that the delay,
the User-Agent and the auth mode are decided in exactly one place. Swapping in
`requests` later means rewriting only this file.
"""
from __future__ import annotations

import base64
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from config import RedditCfg

WWW = "https://www.reddit.com"
OAUTH = "https://oauth.reddit.com"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

# Share-link resolution talks to the public HTML site, which rejects the
# API-style User-Agent that oauth.reddit.com expects.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class RedditBlocked(RuntimeError):
    """Reddit refused the request (403/429 after retries)."""


def is_transient(exc: BaseException) -> bool:
    """Would the same request plausibly succeed later?

    Server errors, rate limits, timeouts, DNS/connection failures and "not
    indexed yet" from the archives are temporary; a 404 or a malformed link is not.
    """
    # Imported lazily: the backends import this module.
    from backend_arctic import ArcticUnavailable
    from backend_pullpush import PullPushUnavailable
    if isinstance(exc, (ArcticUnavailable, PullPushUnavailable)):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError,
                        http.client.HTTPException, json.JSONDecodeError)):
        return True
    return bool(getattr(exc, "transient", False))


class RedditHttp:
    def __init__(self, cfg: RedditCfg, log=None):
        self.cfg = cfg
        self.log = log or (lambda *a, **k: None)
        self._last_request = 0.0
        self._token = ""
        self._token_expiry = 0.0

    # ---------- low level ----------

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_request
        if gap < self.cfg.request_delay:
            time.sleep(self.cfg.request_delay - gap)
        self._last_request = time.monotonic()

    def _headers(self, authed: bool) -> dict[str, str]:
        headers = {"User-Agent": self.cfg.user_agent, "Accept": "application/json"}
        if authed:
            headers["Authorization"] = f"bearer {self._access_token()}"
        return headers

    def _open(self, req: urllib.request.Request, *, allow_retry: bool = True):
        delay = 2.0
        attempts = self.cfg.retries if allow_retry else 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                return urllib.request.urlopen(req, timeout=self.cfg.timeout)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in (429, 500, 502, 503, 504) and attempt < attempts:
                    self.log(f"HTTP {exc.code} on {req.full_url} - retry {attempt} in {delay:.0f}s")
                    time.sleep(delay)
                    delay *= 2
                    continue
                if exc.code in (401, 403, 429):
                    raise RedditBlocked(
                        f"Reddit returned HTTP {exc.code} for {req.full_url}. "
                        "Reddit blocks unauthenticated JSON reads: set REDDIT_CLIENT_ID "
                        "and REDDIT_CLIENT_SECRET in .env from a 'script' app created at "
                        "https://www.reddit.com/prefs/apps"
                    ) from exc
                raise
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                if attempt < attempts:
                    self.log(f"Network error on {req.full_url}: {exc} - retry {attempt} in {delay:.0f}s")
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
        raise last_error  # unreachable, keeps type checkers happy

    # ---------- auth ----------

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        cfg = self.cfg
        if cfg.grant_type == "password":
            form = {"grant_type": "password",
                    "username": cfg.auth_username,
                    "password": cfg.auth_password}
        else:
            # App-only token: read access to public content, no account password.
            form = {"grant_type": "client_credentials", "scope": "read"}
        body = urllib.parse.urlencode(form).encode()
        basic = base64.b64encode(f"{cfg.client_id}:{cfg.client_secret}".encode()).decode()
        req = urllib.request.Request(
            TOKEN_URL, data=body,
            headers={"User-Agent": cfg.user_agent, "Authorization": f"Basic {basic}"},
        )
        with self._open(req) as resp:
            payload = json.load(resp)
        if "access_token" not in payload:
            raise RedditBlocked(f"OAuth token request failed ({cfg.grant_type}): {payload}")
        self._token = payload["access_token"]
        self._token_expiry = time.time() + float(payload.get("expires_in", 3600))
        self.log(f"Acquired OAuth token via {cfg.grant_type}")
        return self._token

    # ---------- public API ----------

    def api_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a Reddit API path such as '/comments/1abc2de'."""
        authed = self.cfg.use_oauth
        params = {**(params or {}), "raw_json": 1}
        base, suffix = (OAUTH, "") if authed else (WWW, ".json")
        url = f"{base}{path}{suffix}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=self._headers(authed))
        with self._open(req) as resp:
            return json.load(resp)

    def api_post(self, path: str, data: dict[str, Any]) -> Any:
        authed = self.cfg.use_oauth
        base, suffix = (OAUTH, "") if authed else (WWW, ".json")
        body = urllib.parse.urlencode({**data, "raw_json": 1}).encode()
        req = urllib.request.Request(
            f"{base}{path}{suffix}", data=body, headers=self._headers(authed))
        with self._open(req) as resp:
            return json.load(resp)

    def json_get(self, url: str, timeout: int | None = None) -> Any:
        """Plain JSON GET with the same throttle and retry policy, no Reddit auth.

        Used for the Arctic Shift backend, which needs no credentials.
        """
        req = urllib.request.Request(url, headers={
            "User-Agent": self.cfg.user_agent, "Accept": "application/json"})
        original = self.cfg.timeout
        if timeout:
            self.cfg.timeout = timeout
        try:
            with self._open(req) as resp:
                return json.load(resp)
        finally:
            self.cfg.timeout = original

    def final_url(self, url: str) -> str:
        """Follow redirects and report where a share link actually lands.

        This hits the public HTML site, not the API, and the HTML site refuses
        the API-style User-Agent - so it gets browser headers instead.
        """
        req = urllib.request.Request(url, headers=BROWSER_HEADERS, method="GET")
        with self._open(req) as resp:
            return resp.geturl()
