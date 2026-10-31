from __future__ import annotations

from kessler.api.middleware import merge_min_lsn


def _min_lsn(headers: list[tuple[bytes, bytes]]) -> bytes | None:
    return next((v for k, v in headers if k == b"x-kessler-min-lsn"), None)


def test_without_the_cookie_headers_are_untouched():
    h = [(b"accept", b"*/*")]
    assert merge_min_lsn(h) is h


def test_cookie_raises_an_older_or_missing_header():
    assert _min_lsn(merge_min_lsn([(b"cookie", b"ks_csrf=x; ks_lsn=0/3A00010")])) == b"0/3A00010"
    h = [(b"x-kessler-min-lsn", b"0/3000000"), (b"cookie", b"ks_lsn=0/3A00010")]
    assert _min_lsn(merge_min_lsn(h)) == b"0/3A00010"


def test_a_newer_header_wins_over_the_cookie():
    h = [(b"x-kessler-min-lsn", b"1/0"), (b"cookie", b"ks_lsn=0/3A00010")]
    assert merge_min_lsn(h) is h


def test_malformed_values_are_ignored():
    for bad in (b"ks_lsn=garbage", b"ks_lsn=0/1\r\nx-evil: 1", b"ks_lsn="):
        h = [(b"cookie", bad)]
        assert merge_min_lsn(h) is h
