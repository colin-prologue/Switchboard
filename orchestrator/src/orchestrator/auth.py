"""GitHub credential providers (issue #10 — adopt GitHub App identity).

One contract, two providers:

- StaticTokenProvider          — a fixed token (the dogfood personal-token
                                 fallback: `export GITHUB_TOKEN="$(gh auth token)"`).
- AppInstallationTokenProvider — mints a GitHub App *installation* token from the
                                 App id + private key + installation id, caches it,
                                 and re-mints shortly before hourly expiry or after
                                 a 401. Only the App private key lives at rest — a
                                 key, never a long-lived access token.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Callable

import httpx
import jwt

# App JWTs: `iat` backdated to tolerate minor clock drift; short `exp` well under
# GitHub's hard 10-minute ceiling.
_JWT_IAT_BACKDATE_SECONDS = 60
_JWT_EXPIRY_SECONDS = 540

# Re-mint this many seconds before the installation token's stated expiry so an
# in-flight request never races the hourly boundary.
_EXPIRY_SKEW_SECONDS = 300


def _parse_expiry(iso8601: str) -> float:
    """GitHub `expires_at` (e.g. '2026-07-06T03:04:03Z') -> epoch seconds."""
    return datetime.fromisoformat(iso8601.replace("Z", "+00:00")).timestamp()


def build_installation_jwt(*, app_id: str, private_key_pem: str, now: float) -> str:
    """Return an RS256 App JWT (iss=app_id) for minting installation tokens."""
    payload = {
        "iat": int(now) - _JWT_IAT_BACKDATE_SECONDS,
        "exp": int(now) + _JWT_EXPIRY_SECONDS,
        "iss": app_id,
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


class StaticTokenProvider:
    """Returns a fixed token; nothing to refresh."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def token(self, *, min_ttl: float = 0.0) -> str:
        return self._token  # a static token has no expiry to honor min_ttl against

    def invalidate(self) -> None:
        """No-op: a static token cannot be re-minted (401 recovery contract)."""


class AppInstallationTokenProvider:
    """Mints a GitHub App installation token from the App key + installation id."""

    def __init__(
        self,
        *,
        app_id: str,
        private_key_pem: str,
        installation_id: str,
        client: httpx.AsyncClient,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._app_id = app_id
        self._pem = private_key_pem
        self._installation_id = installation_id
        self._client = client
        self._now = now
        self._cached: str | None = None
        self._expires_at: float = 0.0

    async def token(self, *, min_ttl: float = 0.0) -> str:
        """Return a token valid for at least max(skew, min_ttl) seconds.

        min_ttl lets a caller demand a longer horizon than the tracker's
        request-scoped skew — e.g. an agent turn whose subprocess env freezes
        the token for the whole turn (Codex PR #42 P1). A min_ttl at or beyond
        the token's full lifetime yields a fresh mint every call (never loops —
        one mint per call), which still cannot cover a turn longer than the
        mint's own validity; that residual is documented in AgDR-009.
        """
        horizon = max(_EXPIRY_SKEW_SECONDS, min_ttl)
        if self._cached is None or self._now() >= self._expires_at - horizon:
            await self._mint()
        return self._cached

    async def _mint(self) -> None:
        app_jwt = build_installation_jwt(
            app_id=self._app_id, private_key_pem=self._pem, now=self._now()
        )
        resp = await self._client.post(
            f"https://api.github.com/app/installations/{self._installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
            },
        )
        # Fail as an httpx error callers can classify — never a KeyError from
        # the missing "token" field of an error body.
        resp.raise_for_status()
        data = resp.json()
        self._cached = data["token"]
        self._expires_at = _parse_expiry(data["expires_at"])

    def invalidate(self) -> None:
        """Drop the cached token so the next `token()` re-mints (e.g. after a 401)."""
        self._cached = None
        self._expires_at = 0.0
