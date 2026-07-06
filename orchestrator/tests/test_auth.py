"""Tests for GitHub credential providers (issue #10 — App identity).

All HTTP is mocked via httpx.MockTransport — no network access. RSA keypairs
are generated per-test so nothing depends on the real App private key.
"""

from __future__ import annotations

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from orchestrator.auth import (
    AppInstallationTokenProvider,
    StaticTokenProvider,
    build_installation_jwt,
)

# 2000-01-01T00:00:00Z and one hour later, as epoch + ISO for expiry tests.
_T0 = 946684800.0
_T0_PLUS_1H_ISO = "2000-01-01T01:00:00Z"


def _mint_transport(records: list, token: str = "ghs_minted",
                    expires_at: str = _T0_PLUS_1H_ISO) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        records.append(request)
        return httpx.Response(201, json={"token": token, "expires_at": expires_at})
    return httpx.MockTransport(handler)


def _app_provider(*, records, client=None, now, installation_id="99"):
    priv, _ = _keypair()
    client = client or httpx.AsyncClient(transport=_mint_transport(records))
    return AppInstallationTokenProvider(
        app_id="4225392", private_key_pem=priv, installation_id=installation_id,
        client=client, now=now,
    )


def _keypair() -> tuple[str, str]:
    """Ephemeral RSA keypair (PEM strings) so tests never touch the real key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv, pub


async def test_static_provider_returns_its_token():
    provider = StaticTokenProvider("ghp_fixed_token")
    assert await provider.token() == "ghp_fixed_token"


def test_build_jwt_signs_with_app_id_and_bounded_expiry():
    priv, pub = _keypair()
    now = 1_700_000_000
    token = build_installation_jwt(app_id="4225392", private_key_pem=priv, now=now)

    # Decodes only if signed correctly with RS256 by the matching key.
    claims = jwt.decode(token, pub, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == "4225392"
    assert claims["iat"] <= now  # backdated to tolerate clock drift
    assert now < claims["exp"] <= now + 600  # GitHub caps App JWTs at 10 minutes


async def test_app_provider_mints_and_returns_token():
    records: list[httpx.Request] = []
    provider = _app_provider(records=records, now=lambda: _T0)
    tok = await provider.token()
    assert tok == "ghs_minted"
    assert len(records) == 1
    req = records[0]
    assert req.method == "POST"
    assert req.url.path == "/app/installations/99/access_tokens"
    assert req.headers["Authorization"].startswith("Bearer ")


async def test_app_provider_caches_within_validity():
    records: list[httpx.Request] = []
    provider = _app_provider(records=records, now=lambda: _T0)
    await provider.token()
    await provider.token()
    assert len(records) == 1  # second call served from cache, no re-mint


async def test_app_provider_remints_within_expiry_skew():
    records: list[httpx.Request] = []
    clock = {"t": _T0}
    provider = _app_provider(records=records, now=lambda: clock["t"])
    await provider.token()  # valid until _T0 + 3600
    clock["t"] = _T0 + 3600 - 299  # within the 300s re-mint skew of expiry
    await provider.token()
    assert len(records) == 2  # re-minted early, before the hourly boundary


async def test_invalidate_forces_remint_on_next_call():
    records: list[httpx.Request] = []
    provider = _app_provider(records=records, now=lambda: _T0)
    await provider.token()
    provider.invalidate()  # e.g. after a 401
    await provider.token()
    assert len(records) == 2


async def test_static_provider_invalidate_is_noop():
    # The tracker calls invalidate() on any 401; a static personal token has
    # nothing to re-mint, so the call must be a safe no-op.
    provider = StaticTokenProvider("ghp_fixed_token")
    provider.invalidate()
    assert await provider.token() == "ghp_fixed_token"


async def test_mint_failure_raises_httpx_status_error():
    # A failed mint (e.g. revoked installation -> 401/404) must surface as an
    # httpx error the tracker can wrap into TrackerError — never a bare
    # KeyError from the missing "token" field.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    priv, _ = _keypair()
    provider = AppInstallationTokenProvider(
        app_id="4225392", private_key_pem=priv, installation_id="99",
        client=client, now=lambda: _T0,
    )
    with pytest.raises(httpx.HTTPStatusError):
        await provider.token()
