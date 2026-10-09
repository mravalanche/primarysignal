"""Bounded plain-text result of an isolated article retrieval."""

import re
from dataclasses import dataclass

from primary_signal.identity.urls import identify_url
from primary_signal.retrieval.extract import (
    MAX_BODY_BYTES,
    MAX_TEXT_CHARS,
    MAX_TITLE_CHARS,
    ArticleExtraction,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_REDIRECTS = 5
MAX_ARTICLE_URL_CHARS = 8 * 1024


@dataclass(frozen=True, slots=True)
class RedirectHop:
    """One validated redirect destination and its preceding HTTP status."""

    status: int
    url: str

    def __post_init__(self) -> None:
        if self.status not in (301, 302, 303, 307, 308):
            raise ValueError("invalid article redirect status")
        if len(self.url) > MAX_ARTICLE_URL_CHARS:
            raise ValueError("article redirect URL exceeds its bound")
        identify_url(self.url)


@dataclass(frozen=True, slots=True)
class ArticleFetchResult:
    """Article metadata and inert extracted text; raw HTML never crosses the API."""

    status: int
    final_url: str
    redirect_chain: tuple[RedirectHop, ...]
    content_type: str | None
    decoded_byte_count: int
    raw_response_hash: str | None
    extraction: ArticleExtraction | None
    etag: str | None
    last_modified: str | None

    def __post_init__(self) -> None:
        if self.status not in (200, 304):
            raise ValueError("article fetch result must be HTTP 200 or 304")
        if len(self.final_url) > MAX_ARTICLE_URL_CHARS:
            raise ValueError("article final URL exceeds its bound")
        identify_url(self.final_url)
        if len(self.redirect_chain) > MAX_REDIRECTS:
            raise ValueError("article redirect chain exceeds its bound")
        if self.redirect_chain and self.redirect_chain[-1].url != self.final_url:
            raise ValueError("article redirect chain does not end at final URL")
        if self.content_type is not None and (
            len(self.content_type) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in self.content_type)
        ):
            raise ValueError("invalid article content type")
        if any(
            value is not None
            and (
                len(value) > 4096
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            )
            for value in (self.etag, self.last_modified)
        ):
            raise ValueError("invalid article validator")
        if self.decoded_byte_count < 0 or self.decoded_byte_count > MAX_BODY_BYTES:
            raise ValueError("article response byte count exceeds its bound")
        if self.status == 200:
            if (
                self.decoded_byte_count == 0
                or self.raw_response_hash is None
                or not _SHA256.fullmatch(self.raw_response_hash)
                or self.extraction is None
                or self.content_type is None
                or not self.extraction.text
                or len(self.extraction.text) > MAX_TEXT_CHARS
                or (
                    self.extraction.title is not None
                    and len(self.extraction.title) > MAX_TITLE_CHARS
                )
                or self.extraction.word_count < 0
                or not _SHA256.fullmatch(self.extraction.normalized_content_hash)
            ):
                raise ValueError("article HTTP 200 requires extracted content")
        elif (
            self.decoded_byte_count
            or self.raw_response_hash is not None
            or self.extraction is not None
        ):
            raise ValueError("article HTTP 304 must not contain extracted content")
