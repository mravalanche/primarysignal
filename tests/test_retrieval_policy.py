"""The URL policy rejects dangerous names and every forbidden DNS answer."""

import ipaddress
from collections.abc import Sequence
from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from primary_signal.retrieval.policy import (
    IANA_SPECIAL_REGISTRY_DATE,
    PolicyError,
    validate_target,
)


def _public_resolver(host: str, port: int) -> Sequence[str]:
    assert host == "xn--bcher-kva.example"
    assert port == 443
    return ["8.8.8.8", "2606:4700:4700::1111", "8.8.8.8"]


def test_canonical_name_and_immutable_pinned_answers() -> None:
    target = validate_target("https://Bücher.example:443/über?q=é#ignored", _public_resolver)
    assert target.scheme == "https"
    assert target.hostname == "xn--bcher-kva.example"
    assert target.port == 443
    assert target.path_and_query == "/%C3%BCber?q=%C3%A9"
    assert target.addresses == (
        ipaddress.IPv4Address("8.8.8.8"),
        ipaddress.IPv6Address("2606:4700:4700::1111"),
    )
    assert "q=" not in repr(target)
    with pytest.raises(FrozenInstanceError):
        target.hostname = "other.example"  # type: ignore[misc]


@pytest.mark.parametrize(
    "url",
    [
        "http://user@public.example/",
        "http://user:REPLACE_ME@public.example/",  # pragma: allowlist secret
        "file:///etc/passwd",
        "//public.example/",
        "http://public.example:81/",
        "https://public.example:80/",
        "https://public.example:/",
        "https://public.example:abc/",
        "https://public.example./",
        "https://public.example/line\rbreak",
        "https://public.example/%0d%0aHeader:evil",
        "https://public.example/%GG",
        "https://public.example\\@private.example/",
        "https://example。com/",
        "https://bad_name.example/",
        "https://127.1/",
        "https://2130706433/",
        "https://0177.0.0.1/",
        "https://0x7f000001/",
        "https://0x7f.0.0.1/",
        "https://[fe80::1%25eth0]/",
        "https://[2001:0DB8::1]/",
        "https://[2001:db8:0:0::1]/",
        "https://[2001:db8::1",
        "https://public.example]",
        "https://localhost/",
        "https://2001:db8::1/",
    ],
)
def test_rejects_unsafe_url_forms_before_dns(url: str) -> None:
    def resolver(_host: str, _port: int) -> Sequence[str]:
        pytest.fail("DNS must not run for a rejected URL")

    with pytest.raises(PolicyError):
        validate_target(url, resolver)


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.1",
        "10.0.0.1",
        "100.64.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "172.16.0.1",
        "192.0.0.9",
        "192.0.2.1",
        "192.31.196.1",
        "192.52.193.1",
        "192.88.99.1",
        "192.168.0.1",
        "192.175.48.1",
        "198.18.0.1",
        "198.51.100.1",
        "203.0.113.1",
        "224.0.0.1",
        "240.0.0.1",
        "::1",
        "::ffff:8.8.8.8",
        "64:ff9b::808:808",
        "64:ff9b:1::1",
        "100::1",
        "100:0:0:1::1",
        "2001::1",
        "2001:db8::1",
        "2002::1",
        "3fff::1",
        "5f00::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
    ],
)
def test_denies_snapshot_special_ranges(address: str) -> None:
    assert IANA_SPECIAL_REGISTRY_DATE == "2025-10-09"
    with pytest.raises(PolicyError, match="forbidden_address"):
        validate_target("https://public.example/", lambda _host, _port: [address])


def test_mixed_dns_answer_fails_whole_target() -> None:
    with pytest.raises(PolicyError, match="forbidden_address"):
        validate_target(
            "https://public.example/",
            lambda _host, _port: ["8.8.8.8", "127.0.0.1"],
        )


@pytest.mark.parametrize("answers", [[], ["another.example"], ["8.8.8.8"] * 33])
def test_dns_answers_must_be_bounded_literal_addresses(answers: list[str]) -> None:
    with pytest.raises(PolicyError, match="dns_answers"):
        validate_target("https://public.example/", lambda _host, _port: answers)


def test_dns_answer_must_be_text_ip_literal() -> None:
    with pytest.raises(PolicyError, match="dns_answers"):
        validate_target(
            "https://public.example/",
            lambda _host, _port: cast("Sequence[str]", [134744072]),
        )


def test_resolver_failure_is_sanitized() -> None:
    def resolver(_host: str, _port: int) -> Sequence[str]:
        raise RuntimeError("private DNS detail")

    with pytest.raises(PolicyError, match="dns_error") as caught:
        validate_target("https://public.example/?sensitive=REPLACE_ME", resolver)
    assert "private" not in str(caught.value)
    assert "sensitive" not in str(caught.value)


def test_canonical_public_literal_skips_dns() -> None:
    def resolver(_host: str, _port: int) -> Sequence[str]:
        pytest.fail("literal address must not resolve again")

    target = validate_target("https://[2606:4700:4700::1111]/", resolver)
    assert target.hostname == "2606:4700:4700::1111"
    assert target.addresses == (ipaddress.IPv6Address("2606:4700:4700::1111"),)


def test_canonical_public_ipv4_literal_skips_dns() -> None:
    def resolver(_host: str, _port: int) -> Sequence[str]:
        pytest.fail("literal address must not resolve again")

    target = validate_target("http://8.8.8.8:80/feed", resolver)
    assert target.hostname == "8.8.8.8"
    assert target.addresses == (ipaddress.IPv4Address("8.8.8.8"),)
    assert target.path_and_query == "/feed"


def test_url_byte_limit_rejects_long_ascii_and_multibyte_paths() -> None:
    def resolver(_host: str, _port: int) -> Sequence[str]:
        pytest.fail("oversized URL must not resolve")

    for url in ("https://public.example/" + "a" * 8192, "https://public.example/" + "é" * 4100):
        with pytest.raises(PolicyError, match="url"):
            validate_target(url, resolver)


def test_url_with_unpaired_surrogate_rejected_before_dns() -> None:
    def resolver(_host: str, _port: int) -> Sequence[str]:
        pytest.fail("invalid Unicode must not resolve")

    with pytest.raises(PolicyError, match="url"):
        validate_target("https://public.example/\ud800", resolver)
