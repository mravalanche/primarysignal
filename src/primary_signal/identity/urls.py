"""Conservative, versioned lexical URL identity."""

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from ipaddress import IPv6Address
from urllib.parse import urlsplit

import idna

URL_NORMALIZATION_VERSION = 1
MAX_URL_BYTES = 8 * 1024

_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class InvalidUrl(ValueError):
    """The value is not a supported, well-formed web URL."""


@dataclass(frozen=True, slots=True)
class UrlIdentity:
    """A stable URL identity under an explicit normalization version."""

    normalized_url: str = field(repr=False)
    url_hash: str
    normalization_version: int


def _invalid() -> InvalidUrl:
    # Never include the submitted value: URLs commonly contain sensitive queries.
    return InvalidUrl("The value is not a supported, well-formed web URL.")


def _validate_characters(value: str) -> None:
    if "\\" in value:
        raise _invalid()
    for character in value:
        codepoint = ord(character)
        category = unicodedata.category(character)
        if codepoint == 0x7F or character.isspace() or category.startswith("C"):
            raise _invalid()


def _require_string(value: object) -> str:
    if not isinstance(value, str):
        raise _invalid()
    # The character check avoids encoding a very large Unicode value merely to
    # discover that it exceeds the byte limit.
    if len(value) > MAX_URL_BYTES:
        raise _invalid()
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise _invalid() from None
    if len(encoded) > MAX_URL_BYTES:
        raise _invalid()
    return value


def _validate_percent_escapes(*components: str) -> None:
    if any(_INVALID_PERCENT_ESCAPE.search(component) is not None for component in components):
        raise _invalid()


def _parse_port(raw_port: str | None) -> tuple[int | None, str | None]:
    if raw_port is None:
        return None, None
    if not raw_port or len(raw_port) > 5 or not raw_port.isascii() or not raw_port.isdecimal():
        raise _invalid()
    port = int(raw_port, 10)
    if not 1 <= port <= 65535:
        raise _invalid()
    return port, raw_port


def _normalize_authority(authority: str, scheme: str) -> str:
    if not authority or "@" in authority:
        raise _invalid()

    if authority.startswith("["):
        closing_bracket = authority.find("]")
        if closing_bracket < 0 or "]" in authority[closing_bracket + 1 :]:
            raise _invalid()
        raw_host = authority[1:closing_bracket]
        suffix = authority[closing_bracket + 1 :]
        if not raw_host or "%" in raw_host or (suffix and not suffix.startswith(":")):
            raise _invalid()
        raw_port = suffix[1:] if suffix else None
        try:
            IPv6Address(raw_host)
        except ValueError:
            raise _invalid() from None
        normalized_host = f"[{raw_host.lower()}]"
    else:
        if "[" in authority or "]" in authority:
            raise _invalid()
        if authority.count(":") > 1:
            raise _invalid()
        if ":" in authority:
            raw_host, raw_port = authority.rsplit(":", 1)
        else:
            raw_host, raw_port = authority, None
        if not raw_host:
            raise _invalid()
        lowered_host = raw_host.lower()
        labels = lowered_host.split(".")
        if any(not label for label in labels[:-1]) or (not labels[-1] and len(labels) == 1):
            raise _invalid()
        try:
            normalized_host = idna.encode(
                lowered_host,
                uts46=True,
                std3_rules=True,
                transitional=False,
            ).decode("ascii")
        except idna.IDNAError:
            raise _invalid() from None

    port, original_port = _parse_port(raw_port)
    if port == {"http": 80, "https": 443}[scheme]:
        original_port = None
    return normalized_host if original_port is None else f"{normalized_host}:{original_port}"


def identify_url(value: str) -> UrlIdentity:
    """Return the version 1 lexical identity for an absolute HTTP(S) URL.

    This performs no DNS or address-policy work. A returned identity is never
    permission to retrieve the URL; the separate SSRF policy must validate the
    canonical host, port, resolved addresses and every redirect.
    """

    value = _require_string(value)
    _validate_characters(value)
    try:
        parsed = urlsplit(value)
    except UnicodeError, ValueError:
        raise _invalid() from None

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc:
        raise _invalid()
    authority = _normalize_authority(parsed.netloc, scheme)
    _validate_percent_escapes(parsed.path, parsed.query, parsed.fragment)

    before_fragment = value.split("#", 1)[0]
    has_query_delimiter = "?" in before_fragment
    normalized_url = f"{scheme}://{authority}{parsed.path}"
    if has_query_delimiter:
        normalized_url = f"{normalized_url}?{parsed.query}"

    return UrlIdentity(
        normalized_url=normalized_url,
        url_hash=hashlib.sha256(normalized_url.encode("utf-8")).hexdigest(),
        normalization_version=URL_NORMALIZATION_VERSION,
    )
