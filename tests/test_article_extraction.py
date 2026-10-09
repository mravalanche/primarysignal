"""Synthetic, bounded article HTML extraction."""

import pytest

from primary_signal.retrieval.extract import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    NORMALIZATION_VERSION,
    InvalidArticle,
    extract_article_html,
)


def test_extracts_article_text_without_navigation_or_executable_content() -> None:
    body = b"""<!doctype html><html><head><title> Notice &amp; update </title>
      <style>do not show</style></head><body><header>Header text</header>
      <nav>Navigation</nav><main><article><h1>Notice</h1>
      <p>First &amp; second sentence.</p><script>dangerous()</script>
      <p hidden>Hidden</p><p aria-hidden="true">Inaccessible</p>
      <p style="display: none">Invisible</p><template>Template</template>
      <noscript>Fallback</noscript><svg><text>Vector</text></svg>
      <form>Form text</form><aside>Related</aside><footer>Footer</footer>
      </article></main></body></html>"""
    result = extract_article_html(body, "text/html; charset=UTF-8")
    assert result.title == "Notice & update"
    assert result.text == "Notice\nFirst & second sentence."
    assert result.word_count == 5
    assert len(result.normalized_content_hash) == 64
    assert (NORMALIZATION_VERSION, EXTRACTOR_NAME, EXTRACTOR_VERSION) == (
        1,
        "primary_signal_html",
        "1",
    )


def test_malformed_html_is_tolerated_and_main_is_preferred() -> None:
    result = extract_article_html(
        b"<html><body>Outside<main><p>Useful <b>words</p></main></body></html>",
        "text/html",
    )
    assert result.text == "Useful words"


def test_body_fallback_and_stable_hash() -> None:
    one = extract_article_html(b"<body><p>A  short\tstory.</p></body>", "text/html")
    two = extract_article_html(b"<body><p>A short story.</p></body>", "text/html")
    assert one.text == two.text == "A short story."
    assert one.normalized_content_hash == two.normalized_content_hash
    different = extract_article_html(b"<body><p>A different story.</p></body>", "text/html")
    assert one.normalized_content_hash != different.normalized_content_hash


@pytest.mark.parametrize(
    ("body", "mime", "code"),
    [
        (b"<p>words</p>", None, "unsupported_content_type"),
        (b"<p>words</p>", "text/plain", "unsupported_content_type"),
        (b"<p>words</p>", "text/html; charset=iso-8859-1", "unsupported_charset"),
        (b"<p>words</p>", "text/html; charset=utf-8; charset=utf-8", "unsupported_charset"),
        (b"\xff", "text/html", "invalid_encoding"),
        (b"<article>A\x00B</article>", "text/html", "invalid_html"),
        (b"<article>A\x1bB</article>", "text/html", "invalid_html"),
        (b"<article>A\x7fB</article>", "text/html", "invalid_html"),
        (b"", "text/html", "invalid_body_size"),
        (b"x" * (2 * 1024 * 1024 + 1), "text/html", "invalid_body_size"),
        (b"<script>only script</script>", "text/html", "empty_content"),
        (b"<article>" + b"x" * 200_001 + b"</article>", "text/html", "output_too_large"),
        (b"<title>" + b"x" * 513 + b"</title><p>text</p>", "text/html", "output_too_large"),
        (b"<div>" * 129 + b"words", "text/html", "html_too_complex"),
        (b"<br>" * 100_001 + b"words", "text/html", "html_too_complex"),
    ],
)
def test_unsupported_or_excessive_input_is_rejected(
    body: bytes, mime: str | None, code: str
) -> None:
    with pytest.raises(InvalidArticle) as caught:
        extract_article_html(body, mime)
    assert caught.value.code == code
