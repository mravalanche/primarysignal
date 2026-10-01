"""Tests for conservative, versioned lexical URL identity."""

import hashlib
import re
import unicodedata
from typing import Any

import pytest

from primary_signal.identity import (
    MAX_URL_BYTES,
    URL_NORMALIZATION_VERSION,
    InvalidUrl,
    identify_url,
)


@pytest.mark.parametrize(
    ("submitted", "normalized"),
    [
        (
            "HTTP://BÜCHER.Example:080/a%2fb?b=2&a=1#section",
            "http://xn--bcher-kva.example/a%2fb?b=2&a=1",
        ),
        ("https://Public.Example:443", "https://public.example"),
        ("http://public.example:443/a", "http://public.example:443/a"),
        (
            "https://public.example/a/../b//c?x=1+x&x=%2f",
            "https://public.example/a/../b//c?x=1+x&x=%2f",
        ),
        ("https://public.example/path?", "https://public.example/path?"),
        (
            "https://[2001:0DB8::1]:443/report#details",
            "https://[2001:0db8::1]/report",
        ),
        ("http://192.0.2.10/", "http://192.0.2.10/"),
        ("https://localhost:0081", "https://localhost:0081"),
        ("https://public.example./", "https://public.example./"),
        ("https://public.example/ü?q=λ", "https://public.example/ü?q=λ"),
        ("https://example。com/", "https://example.com/"),
        ("http://①②⑦.⓪.⓪.①/", "http://127.0.0.1/"),
    ],
)
def test_identify_url_frozen_examples(submitted: str, normalized: str) -> None:
    identity = identify_url(submitted)

    assert identity.normalized_url == normalized
    assert identity.normalization_version == URL_NORMALIZATION_VERSION == 1
    assert identity.url_hash == hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", identity.url_hash)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("HTTP://PUBLIC.EXAMPLE/a", "http://public.example/a"),
        ("https://faß.de/", "https://xn--fa-hia.de/"),
        ("http://public.example:080/a", "http://public.example/a"),
        ("https://public.example/a#one", "https://public.example/a#two"),
    ],
)
def test_equivalent_urls_share_an_identity(left: str, right: str) -> None:
    assert identify_url(left) == identify_url(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://public.example", "https://public.example/"),
        ("https://public.example/path", "https://public.example/path?"),
        ("https://public.example/?a=1&b=2", "https://public.example/?b=2&a=1"),
        ("https://public.example/?a=1", "https://public.example/?a=1&a=1"),
        ("https://public.example/?q=a+b", "https://public.example/?q=a%20b"),
        ("https://public.example/%2f", "https://public.example/%2F"),
        ("https://public.example/%2f", "https://public.example//"),
        ("https://public.example/a/../b", "https://public.example/b"),
        ("https://public.example/a//b", "https://public.example/a/b"),
        ("https://public.example./", "https://public.example/"),
        ("https://public.example:81/", "https://public.example:0081/"),
        ("https://public.example/é?q=é", "https://public.example/é?q=é"),
        ("https://[2001:0db8:0:0::1]/", "https://[2001:db8::1]/"),
    ],
)
def test_v1_deliberately_preserves_distinct_syntax(left: str, right: str) -> None:
    assert identify_url(left) != identify_url(right)


@pytest.mark.parametrize(
    "submitted",
    [
        None,
        42,
        "/relative",
        "//public.example/path",
        "ftp://public.example/path",
        "http:///path",
        "http://",
        "http://user@public.example/",
        "http://user:REPLACE_ME@public.example/",  # pragma: allowlist secret
        "http://public.example:/",
        "http://public.example:+80/",
        "http://public.example:-1/",
        "http://public.example:abc/",
        "http://public.example:0/",
        "http://public.example:65536/",
        "http://public.example:000080/",
        "http://[2001:db8::zz]/",
        "http://[2001:db8::1",
        "http://2001:db8::1/",
        "http://[fe80::1%25eth0]/",
        "http://a..example/",
        "http://.example/",
        "http://bad_name.example/",
        "http://public.example\\path",
        "http://public.example/white space",
        "http://public.example/line\nbreak",
        "http://public.example/\x7f",
        "http://public.example/\u0085",
        "http://public.example/\u202e",
        "http://public.example/\u2066",
        "http://public.example/\u2028",
        "http://public.example/\u2029",
        "http://public.example/\ufeff",
        "http://exa\u200bmple.com/",
        "http://public.example/\ue000",
        "http://public.example/%",
        "http://public.example/%2",
        "http://public.example/?q=%GG",
        "http://public.example/#bad%z0",
    ],
)
def test_invalid_urls_are_rejected_without_echoing_input(submitted: Any) -> None:
    with pytest.raises(InvalidUrl) as caught:
        identify_url(submitted)

    assert not isinstance(submitted, str) or submitted not in str(caught.value)


def test_pathological_numeric_port_uses_the_safe_error_contract() -> None:
    submitted = f"http://public.example:{'0' * 4998}80/"

    with pytest.raises(InvalidUrl) as caught:
        identify_url(submitted)

    assert submitted not in str(caught.value)


def test_unicode_normalization_is_not_applied() -> None:
    composed = identify_url("https://public.example/é?q=é")
    decomposed = identify_url(
        f"https://public.example/{unicodedata.normalize('NFD', 'é')}"
        f"?q={unicodedata.normalize('NFD', 'é')}"
    )

    assert composed != decomposed


def test_url_size_is_bounded_by_utf8_bytes() -> None:
    prefix = "https://public.example/"
    accepted = f"{prefix}{'a' * (MAX_URL_BYTES - len(prefix))}"
    too_many_ascii_bytes = f"{accepted}a"
    unicode_prefix = f"{prefix}ü"
    too_many_unicode_bytes = f"{unicode_prefix}{'a' * (MAX_URL_BYTES - len(unicode_prefix))}"

    assert len(accepted.encode()) == MAX_URL_BYTES
    assert identify_url(accepted).normalized_url == accepted
    with pytest.raises(InvalidUrl):
        identify_url(too_many_ascii_bytes)
    assert len(too_many_unicode_bytes) == MAX_URL_BYTES
    assert len(too_many_unicode_bytes.encode()) == MAX_URL_BYTES + 1
    with pytest.raises(InvalidUrl):
        identify_url(too_many_unicode_bytes)


def test_identity_repr_does_not_expose_query_values() -> None:
    identity = identify_url("https://public.example/path?token=public-test-value")

    assert "token" not in repr(identity)
    assert "public-test-value" not in repr(identity)


@pytest.mark.parametrize(
    "submitted",
    [
        "http://127.1/",
        "http://0177.0.0.1/",
        "http://0x7f000001/",
        "http://2130706433/",
        "http://[::ffff:127.0.0.1]/",
        "https://public.example:8443/%2e%2e?q=%0d%0a",
    ],
)
def test_identity_is_not_retrieval_permission(submitted: str) -> None:
    assert identify_url(submitted).normalized_url
