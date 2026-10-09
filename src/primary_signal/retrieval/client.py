"""Bounded processor client for the isolated feed retriever API."""

import base64
import binascii
import json
import re
from time import monotonic
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

from primary_signal.ingestion.feed_handler import FeedFetchError
from primary_signal.ingestion.feed_polls import FeedFetchResult, FeedPollTarget
from primary_signal.retrieval.article import (
    MAX_ARTICLE_URL_CHARS,
    MAX_REDIRECTS,
    ArticleFetchResult,
    RedirectHop,
)
from primary_signal.retrieval.extract import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    MAX_TEXT_CHARS,
    MAX_TITLE_CHARS,
    NORMALIZATION_VERSION,
    ArticleExtraction,
)

MAX_API_RESPONSE_BYTES = 3 * 1024 * 1024
TOTAL_RESPONSE_TIMEOUT_SECONDS = 25.0
READ_TIMEOUT_SECONDS = 23.0
_FETCH_PATH = "/v1/feeds/fetch"
_ARTICLE_FETCH_PATH = "/v1/articles/fetch"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_REMOTE_ERRORS = frozenset(
    {
        "ambiguous_ip",
        "authority",
        "dns_answers",
        "dns_error",
        "fetch_network",
        "fetch_failed",
        "fetch_timeout",
        "forbidden_address",
        "host",
        "http_status",
        "https_downgrade",
        "invalid_redirect",
        "invalid_request",
        "invalid_response",
        "invalid_response_body",
        "invalid_response_headers",
        "noncanonical_ip",
        "peer_mismatch",
        "port",
        "redirect_loop",
        "request_too_large",
        "response_too_large",
        "retriever_busy",
        "scheme",
        "unsupported_content_encoding",
        "unsupported_transfer_encoding",
        "url",
        "unsupported_content_type",
        "unsupported_charset",
        "invalid_body_size",
        "invalid_encoding",
        "html_too_complex",
        "invalid_html",
        "empty_content",
        "output_too_large",
    }
)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _decode_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise FeedFetchError("retriever_protocol") from error
    if not isinstance(value, dict):
        raise FeedFetchError("retriever_protocol")
    return cast(dict[str, Any], value)


def _remote_error(status: int, code: str) -> str:
    if code == "retriever_busy" or status == 429:
        return "rate_limited"
    if code == "fetch_timeout":
        return "dependency_timeout"
    if code in {"fetch_network", "dns_error", "fetch_failed"}:
        return "dependency_unavailable"
    return code


def _check_deadline(deadline: float) -> None:
    if monotonic() >= deadline:
        raise FeedFetchError("dependency_timeout")


class RetrieverClient:
    """FeedFetcher adapter; all public URL retrieval stays in the retriever."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or base_url.rstrip("/") != f"{parsed.scheme}://{parsed.netloc}"
        ):
            raise ValueError("retriever base URL must be a fixed HTTP origin")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=5.0),
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RetrieverClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def fetch(self, target: FeedPollTarget) -> FeedFetchResult:
        payload = self._post(
            _FETCH_PATH,
            {"url": target.url, "etag": target.etag, "last_modified": target.last_modified},
        )
        if set(payload) != {"status", "final_url", "body_base64", "etag", "last_modified"}:
            raise FeedFetchError("retriever_protocol")
        if (
            type(payload["status"]) is not int
            or not isinstance(payload["final_url"], str)
            or not isinstance(payload["body_base64"], str)
            or any(
                value is not None and not isinstance(value, str)
                for value in (payload["etag"], payload["last_modified"])
            )
        ):
            raise FeedFetchError("retriever_protocol")
        try:
            body = base64.b64decode(payload["body_base64"], validate=True)
            return FeedFetchResult(
                status=payload["status"],
                final_url=payload["final_url"],
                body=body,
                etag=payload["etag"],
                last_modified=payload["last_modified"],
            )
        except (ValueError, binascii.Error) as error:
            raise FeedFetchError("retriever_protocol") from error

    def fetch_article(
        self, url: str, etag: str | None = None, last_modified: str | None = None
    ) -> ArticleFetchResult:
        """Fetch inert article text through the fixed internal endpoint."""

        payload = self._post(
            _ARTICLE_FETCH_PATH,
            {"url": url, "etag": etag, "last_modified": last_modified},
        )
        required = {
            "status",
            "final_url",
            "redirect_chain",
            "content_type",
            "decoded_byte_count",
            "raw_response_hash",
            "extracted_title",
            "extracted_text",
            "word_count",
            "normalized_content_hash",
            "extractor_name",
            "extractor_version",
            "normalization_version",
            "etag",
            "last_modified",
        }
        if set(payload) != required:
            raise FeedFetchError("retriever_protocol")
        status = payload["status"]
        final_url = payload["final_url"]
        chain_value = payload["redirect_chain"]
        byte_count = payload["decoded_byte_count"]
        if (
            type(status) is not int
            or status not in (200, 304)
            or not isinstance(final_url, str)
            or len(final_url) > MAX_ARTICLE_URL_CHARS
            or not isinstance(chain_value, list)
            or len(cast(list[Any], chain_value)) > MAX_REDIRECTS
            or type(byte_count) is not int
        ):
            raise FeedFetchError("retriever_protocol")
        for key in ("content_type", "etag", "last_modified"):
            value = payload[key]
            if value is not None and (not isinstance(value, str) or len(value) > 8192):
                raise FeedFetchError("retriever_protocol")
        hops: list[RedirectHop] = []
        try:
            for hop in cast(list[Any], chain_value):
                if not isinstance(hop, dict):
                    raise ValueError("invalid redirect hop")
                hop_data = cast(dict[str, Any], hop)
                if (
                    set(hop_data) != {"status", "url"}
                    or type(hop_data["status"]) is not int
                    or not isinstance(hop_data["url"], str)
                    or len(hop_data["url"]) > MAX_ARTICLE_URL_CHARS
                ):
                    raise ValueError("invalid redirect hop")
                hops.append(RedirectHop(status=hop_data["status"], url=hop_data["url"]))
            if status == 200:
                title = payload["extracted_title"]
                article_text = payload["extracted_text"]
                word_count = payload["word_count"]
                digest = payload["normalized_content_hash"]
                raw_hash = payload["raw_response_hash"]
                if (
                    (
                        title is not None
                        and (not isinstance(title, str) or len(title) > MAX_TITLE_CHARS)
                    )
                    or not isinstance(article_text, str)
                    or not article_text
                    or len(article_text) > MAX_TEXT_CHARS
                    or type(word_count) is not int
                    or word_count < 1
                    or word_count > MAX_TEXT_CHARS
                    or not isinstance(digest, str)
                    or not _SHA256.fullmatch(digest)
                    or not isinstance(raw_hash, str)
                    or not _SHA256.fullmatch(raw_hash)
                    or payload["extractor_name"] != EXTRACTOR_NAME
                    or payload["extractor_version"] != EXTRACTOR_VERSION
                    or type(payload["normalization_version"]) is not int
                    or payload["normalization_version"] != NORMALIZATION_VERSION
                ):
                    raise ValueError("invalid extraction")
                extraction = ArticleExtraction(
                    title=title,
                    text=article_text,
                    word_count=word_count,
                    normalized_content_hash=digest,
                )
            else:
                if any(
                    payload[key] is not None
                    for key in (
                        "raw_response_hash",
                        "extracted_title",
                        "extracted_text",
                        "word_count",
                        "normalized_content_hash",
                        "extractor_name",
                        "extractor_version",
                        "normalization_version",
                    )
                ):
                    raise ValueError("304 included content")
                extraction = None
            return ArticleFetchResult(
                status=status,
                final_url=final_url,
                redirect_chain=tuple(hops),
                content_type=payload["content_type"],
                decoded_byte_count=byte_count,
                raw_response_hash=payload["raw_response_hash"],
                extraction=extraction,
                etag=payload["etag"],
                last_modified=payload["last_modified"],
            )
        except ValueError as error:
            raise FeedFetchError("retriever_protocol") from error

    def _post(self, path: str, data: dict[str, str | None]) -> dict[str, Any]:
        # The monotonic deadline covers the whole exchange. An in-flight read can
        # exceed it by at most the configured HTTPX read timeout.
        deadline = monotonic() + TOTAL_RESPONSE_TIMEOUT_SECONDS
        try:
            _check_deadline(deadline)
            with self._client.stream(
                "POST",
                path,
                json=data,
            ) as response:
                _check_deadline(deadline)
                if response.is_redirect:
                    raise FeedFetchError("retriever_protocol")
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        length = int(content_length)
                    except ValueError as error:
                        raise FeedFetchError("retriever_protocol") from error
                    if length < 0 or length > MAX_API_RESPONSE_BYTES:
                        raise FeedFetchError("retriever_protocol")
                chunks: list[bytes] = []
                length = 0
                for chunk in response.iter_raw():
                    _check_deadline(deadline)
                    length += len(chunk)
                    if length > MAX_API_RESPONSE_BYTES:
                        raise FeedFetchError("retriever_protocol")
                    chunks.append(chunk)
                    _check_deadline(deadline)
                _check_deadline(deadline)
                try:
                    payload = _decode_object(b"".join(chunks))
                except FeedFetchError:
                    if response.status_code == 429:
                        raise FeedFetchError("rate_limited") from None
                    if 500 <= response.status_code <= 599:
                        raise FeedFetchError("dependency_unavailable") from None
                    raise
                if response.status_code != 200:
                    code = payload.get("error")
                    if (
                        set(payload) == {"error"}
                        and isinstance(code, str)
                        and code in _SAFE_REMOTE_ERRORS
                    ):
                        raise FeedFetchError(_remote_error(response.status_code, code))
                    if response.status_code == 429:
                        raise FeedFetchError("rate_limited")
                    if 500 <= response.status_code <= 599:
                        raise FeedFetchError("dependency_unavailable")
                    raise FeedFetchError("retriever_protocol")
                return payload
        except httpx.TimeoutException as error:
            raise FeedFetchError("dependency_timeout") from error
        except httpx.TransportError as error:
            raise FeedFetchError("dependency_unavailable") from error
