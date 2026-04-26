"""Unit tests for ``backend.auth.jwt``."""
from __future__ import annotations

import time

import pytest

from backend.auth.jwt import (
    JWTError,
    create_access_token,
    decode_token,
    refresh_access_token,
)
from backend.config import get_settings


def test_issue_and_decode_roundtrip():
    get_settings.cache_clear()
    token, exp, jti = create_access_token(42, extra_claims={"scope": "web"})
    payload = decode_token(token)
    assert payload["user_id"] == 42
    assert payload["scope"] == "web"
    assert payload["iss"] == get_settings().jwt_issuer
    assert payload["exp"] == exp
    assert payload["jti"] == jti


def test_expired_token_rejected():
    get_settings.cache_clear()
    token, _, _ = create_access_token(1, ttl_seconds=-1)
    with pytest.raises(JWTError):
        decode_token(token)


def test_bad_signature_rejected():
    get_settings.cache_clear()
    token, _, _ = create_access_token(1)
    # Mangle several signature chars so at least one definitely breaks
    # the signature. Mangling a single char is base64-fragile: if the
    # substituted char happens to round-trip to the same byte when
    # decoded, PyJWT silently still verifies the token. Replacing a
    # 5-char slice in the signature guarantees the signature bytes
    # change even if one individual char collides.
    signature = token.rsplit(".", 1)[-1]
    if len(signature) < 6:
        # Defensive: HS256 sigs are always much longer, but don't
        # crash the test on an unexpected edge case.
        tampered = token[:-1] + ("B" if token[-1] != "B" else "C")
    else:
        # Replace chars 2..6 of the signature with a fixed stable pattern
        # that is guaranteed base64-valid and different from the original.
        orig_slice = signature[2:7]
        replacement = "ZZZZZ" if orig_slice != "ZZZZZ" else "AAAAA"
        tampered_sig = signature[:2] + replacement + signature[7:]
        head = token.rsplit(".", 1)[0]
        tampered = f"{head}.{tampered_sig}"
    with pytest.raises(JWTError):
        decode_token(tampered)


def test_refresh_preserves_user_and_claims():
    get_settings.cache_clear()
    token, _, _ = create_access_token(7, extra_claims={"scope": "web"})
    # Sleep >=1 second so the new iat differs — covers "refresh really issues a new token".
    time.sleep(1.05)
    new_token, new_exp, new_jti = refresh_access_token(token)
    assert new_token != token
    payload = decode_token(new_token)
    assert payload["user_id"] == 7
    assert payload["scope"] == "web"
    assert payload["exp"] == new_exp
    assert payload["jti"] == new_jti


def test_refresh_rejects_invalid_token():
    with pytest.raises(JWTError):
        refresh_access_token("not.a.jwt")


@pytest.mark.asyncio
async def test_refresh_endpoint_blacklists_old_token(client):
    r = await client.post(
        "/api/auth/register",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": "password123",
        },
    )
    assert r.status_code == 201

    lr = await client.post(
        "/api/auth/login",
        json={"username_or_email": "alice", "password": "password123"},
    )
    old_token = lr.json()["access_token"]

    # Sleep to guarantee new iat/exp and therefore a distinct token string.
    time.sleep(1.05)
    headers = {"Authorization": f"Bearer {old_token}"}
    rr = await client.post("/api/auth/refresh", headers=headers)
    assert rr.status_code == 200, rr.text
    new_token = rr.json()["access_token"]
    assert new_token != old_token

    # Old token is now blacklisted; new token still works.
    assert (
        await client.get("/api/auth/me", headers=headers)
    ).status_code == 401
    assert (
        await client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {new_token}"},
        )
    ).status_code == 200
