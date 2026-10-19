from __future__ import annotations

import time

import pytest

from kessler.config import Settings
from kessler.db import parse_lsn
from kessler.security import (
    api_key_matches,
    decode_access_token,
    hash_password,
    issue_access_token,
    new_api_key,
    sign_stream_token,
    split_api_key,
    verify_password,
    verify_stream_token,
)

S = Settings()


async def test_argon2_round_trip():
    h = await hash_password("correct horse battery staple")
    assert h.startswith("$argon2id$")
    assert await verify_password(h, "correct horse battery staple")
    assert not await verify_password(h, "wrong")


def test_access_token_round_trip_and_tamper_detection():
    tok = issue_access_token(S, "u-1", "ada", guest=False)
    p = decode_access_token(S, tok)
    assert p is not None and p.user_id == "u-1" and p.handle == "ada" and not p.guest
    assert decode_access_token(S, tok[:-2] + ("A" if tok[-2] != "A" else "B") + tok[-1]) is None
    other = Settings(jwt_secret="another-secret-value")
    assert decode_access_token(other, tok) is None


def test_stream_tokens_bind_topic_user_and_expiry():
    tok = sign_stream_token(S, "analysis:42", "u-1", "r", ttl_s=60)
    claims = verify_stream_token(S, tok)
    assert claims == {"topic": "analysis:42", "user_id": "u-1", "perms": "r", "exp": claims["exp"]}
    assert verify_stream_token(S, tok.replace(".", ".x", 1)) is None
    expired = sign_stream_token(S, "analysis:42", "u-1", "r", ttl_s=-1)
    assert verify_stream_token(S, expired) is None


def test_api_keys_are_prefixed_and_hashed():
    full, prefix, stored = new_api_key()
    assert full.startswith(f"ks_{prefix}_")
    parsed = split_api_key(full)
    assert parsed and parsed[0] == prefix
    assert api_key_matches(stored, parsed[1])
    assert not api_key_matches(stored, parsed[1] + "x")
    assert split_api_key("not-a-key") is None


@pytest.mark.parametrize(
    ("lsn", "value"), [("0/0", 0), ("16/B374D848", (0x16 << 32) | 0xB374D848), (None, None), ("bad", None)]
)
def test_lsn_parsing_orders_wal_positions(lsn, value):
    assert parse_lsn(lsn) == value


def test_production_refuses_dev_secrets():
    with pytest.raises(ValueError):
        Settings(env="prod")
    ok = Settings(env="prod", jwt_secret="x" * 40, stream_secret="y" * 40, cookie_secure=True)
    assert ok.env == "prod"


def test_settings_time_budget():
    assert S.access_ttl_s <= 3600
    assert time.time() > 0
