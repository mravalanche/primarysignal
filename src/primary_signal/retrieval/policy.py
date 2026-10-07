"""Strict URL and DNS policy for the isolated feed retriever.

The deny lists are a conservative snapshot of the IANA IPv4 and IPv6
Special-Purpose Address Registries, last updated 2025-10-09. The transport
must connect to an address in ``ValidatedTarget.addresses`` without a second
lookup, and verify the connected peer address.
"""

import ipaddress
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

import idna

type Address = ipaddress.IPv4Address | ipaddress.IPv6Address
type Resolver = Callable[[str, int], Sequence[str]]

MAX_URL_BYTES = 8192
MAX_DNS_ANSWERS = 32
IANA_SPECIAL_REGISTRY_DATE = "2025-10-09"

# All registry entries are denied, including globally reachable anycast and
# translation prefixes, because they have special routing or protocol meaning.
# Broad parent ranges deliberately cover registry-specific exceptions.
_V4_DENY = tuple(
    ipaddress.IPv4Network(value)
    for value in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.31.196.0/24",
        "192.52.193.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "192.175.48.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
_V6_DENY = tuple(
    ipaddress.IPv6Network(value)
    for value in (
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "100:0:0:1::/64",
        "2001::/23",
        "2001:db8::/32",
        "2002::/16",
        "3fff::/20",
        "5f00::/16",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)
_V6_GLOBAL_UNICAST = ipaddress.IPv6Network("2000::/3")
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ENCODED_CONTROL = re.compile(r"%(?:0[0-9A-Fa-f]|1[0-9A-Fa-f]|7[fF])")
_NUMERIC_LABEL = re.compile(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)")


class PolicyError(ValueError):
    """A retrieval target was rejected; the code contains no submitted URL."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ValidatedTarget:
    """Canonical target and the only addresses the transport may connect to."""

    scheme: str
    hostname: str
    port: int
    path_and_query: str = field(repr=False)
    addresses: tuple[Address, ...]


def _public_address(address: Address) -> bool:
    if isinstance(address, ipaddress.IPv4Address):
        return address.is_global and not any(address in network for network in _V4_DENY)
    return (
        address in _V6_GLOBAL_UNICAST
        and address.is_global
        and not any(address in network for network in _V6_DENY)
    )


def _canonical_host(authority: str) -> tuple[str, str | None, Address | None]:
    if not authority or "@" in authority:
        raise PolicyError("authority")
    if authority.startswith("["):
        closing = authority.find("]")
        if closing < 0:
            raise PolicyError("host")
        raw = authority[1:closing]
        suffix = authority[closing + 1 :]
        if not raw or "%" in raw or (suffix and not suffix.startswith(":")):
            raise PolicyError("host")
        try:
            address = ipaddress.IPv6Address(raw)
        except ValueError:
            raise PolicyError("host") from None
        if raw != str(address):
            raise PolicyError("noncanonical_ip")
        return str(address), suffix[1:] if suffix else None, address

    if "[" in authority or "]" in authority or authority.count(":") > 1:
        raise PolicyError("host")
    raw, separator, port = authority.partition(":")
    if (
        not raw
        or raw.endswith(".")
        or "%" in raw
        or any(dot in raw for dot in ("\u3002", "\uff0e", "\uff61"))
    ):
        raise PolicyError("host")
    if raw.isascii() and re.fullmatch(r"[0-9.]+", raw):
        try:
            address = ipaddress.IPv4Address(raw)
        except ValueError:
            raise PolicyError("ambiguous_ip") from None
        return raw, port if separator else None, address
    # Alternate IPv4 spellings can be interpreted by some socket APIs. Reject
    # numeric final labels and hexadecimal/octal-like numeric host components.
    if _NUMERIC_LABEL.fullmatch(raw.split(".")[-1]) or any(
        label.lower().startswith("0x") for label in raw.split(".")
    ):
        raise PolicyError("ambiguous_ip")
    try:
        host = idna.encode(raw.lower(), uts46=False, std3_rules=True).decode("ascii")
    except idna.IDNAError:
        raise PolicyError("host") from None
    if len(host) > 253 or "." not in host:
        raise PolicyError("host")
    return host, port if separator else None, None


def validate_target(url: str, resolver: Resolver) -> ValidatedTarget:
    """Validate an absolute feed URL and every DNS answer before any connection."""

    if len(url) > MAX_URL_BYTES:
        raise PolicyError("url")
    try:
        if len(url.encode("utf-8")) > MAX_URL_BYTES:
            raise PolicyError("url")
    except UnicodeError:
        raise PolicyError("url") from None
    if "\\" in url or any(
        character.isspace()
        or ord(character) == 127
        or unicodedata.category(character).startswith("C")
        for character in url
    ):
        raise PolicyError("url")
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise PolicyError("url") from None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc:
        raise PolicyError("scheme")
    hostname, raw_port, literal = _canonical_host(parsed.netloc)
    port = 80 if scheme == "http" else 443
    if raw_port is not None:
        if not raw_port or not raw_port.isascii() or not raw_port.isdecimal():
            raise PolicyError("port")
        if int(raw_port, 10) != port:
            raise PolicyError("port")
    if _BAD_ESCAPE.search(parsed.path + parsed.query) or _ENCODED_CONTROL.search(
        parsed.path + parsed.query
    ):
        raise PolicyError("url")
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="/%:@!$&'()*+,;=?-._~")
    path_and_query = path + ("?" + query if "?" in url.split("#", 1)[0] else "")

    if literal is not None:
        candidates = (literal,)
    else:
        try:
            answers = resolver(hostname, port)
        except Exception:
            raise PolicyError("dns_error") from None
        if isinstance(answers, (str, bytes)) or not 0 < len(answers) <= MAX_DNS_ANSWERS:
            raise PolicyError("dns_answers")
        if any(type(answer) is not str for answer in answers):
            raise PolicyError("dns_answers")
        try:
            candidates = tuple(ipaddress.ip_address(answer) for answer in answers)
        except TypeError, ValueError:
            raise PolicyError("dns_answers") from None
    if not all(_public_address(address) for address in candidates):
        raise PolicyError("forbidden_address")
    # Preserve answer order for transport retries; duplicate answers have no value.
    addresses = tuple(dict.fromkeys(candidates))
    return ValidatedTarget(scheme, hostname, port, path_and_query, addresses)
