"""Bounded, deterministic plain-text extraction from fetched article HTML."""

import hashlib
import json
from dataclasses import dataclass
from html.parser import HTMLParser

NORMALIZATION_VERSION = 1
EXTRACTOR_NAME = "primary_signal_html"
EXTRACTOR_VERSION = "1"
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
MAX_TITLE_CHARS = 512
MAX_PARSER_EVENTS = 100_000
MAX_HTML_DEPTH = 128

_EXCLUDED = frozenset(
    {"script", "style", "template", "noscript", "svg", "form", "head", "nav", "aside", "footer"}
)
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_BLOCK = frozenset(
    {
        "article",
        "main",
        "body",
        "p",
        "div",
        "section",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "ul",
        "ol",
        "br",
        "hr",
        "blockquote",
        "pre",
        "tr",
    }
)


class InvalidArticle(ValueError):
    """The response is unsupported or exceeds an extraction limit."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ArticleExtraction:
    title: str | None
    text: str
    word_count: int
    normalized_content_hash: str


class _Buffer:
    def __init__(self, limit: int) -> None:
        self.parts: list[str] = []
        self.length = 0
        self.limit = limit

    def append(self, value: str) -> None:
        self.length += len(value)
        if self.length > self.limit:
            raise InvalidArticle("output_too_large")
        self.parts.append(value)

    def normalized(self) -> str:
        return "\n".join(
            line for raw in "".join(self.parts).splitlines() if (line := " ".join(raw.split()))
        )


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.events = 0
        self.title = _Buffer(MAX_TITLE_CHARS)
        self.article = _Buffer(MAX_TEXT_CHARS)
        self.main = _Buffer(MAX_TEXT_CHARS)
        self.body = _Buffer(MAX_TEXT_CHARS)
        self.all_text = _Buffer(MAX_TEXT_CHARS)

    def _event(self) -> None:
        self.events += 1
        if self.events > MAX_PARSER_EVENTS:
            raise InvalidArticle("html_too_complex")

    def _active(self, tag: str) -> bool:
        return any(name == tag for name, _ in self.stack)

    def _visible(self) -> bool:
        return not any(hidden for _, hidden in self.stack)

    def _break(self) -> None:
        if not self._visible():
            return
        for tag, buffer in (("article", self.article), ("main", self.main), ("body", self.body)):
            if self._active(tag):
                buffer.append("\n")
        self.all_text.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._event()
        tag = tag.lower()
        if tag in _BLOCK:
            self._break()
        if tag in _VOID:
            return
        attributes = dict(attrs)
        style = (attributes.get("style") or "").lower().replace(" ", "")
        hidden = (
            tag in _EXCLUDED
            or "hidden" in attributes
            or (attributes.get("aria-hidden") or "").lower() == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        )
        self.stack.append((tag, hidden))
        if len(self.stack) > MAX_HTML_DEPTH:
            raise InvalidArticle("html_too_complex")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        self._event()
        tag = tag.lower()
        if tag in _BLOCK:
            self._break()
        for position in range(len(self.stack) - 1, -1, -1):
            if self.stack[position][0] == tag:
                del self.stack[position:]
                break

    def handle_data(self, data: str) -> None:
        self._event()
        if self.stack and self.stack[-1][0] == "title" and not self._active("body"):
            self.title.append(data)
            return
        if not self._visible():
            return
        for tag, buffer in (("article", self.article), ("main", self.main), ("body", self.body)):
            if self._active(tag):
                buffer.append(data)
        self.all_text.append(data)


def _charset(content_type: str | None) -> str:
    if content_type is None:
        raise InvalidArticle("unsupported_content_type")
    parts = [part.strip() for part in content_type.split(";")]
    if parts[0].lower() not in {"text/html", "application/xhtml+xml"}:
        raise InvalidArticle("unsupported_content_type")
    charset = "utf-8"
    seen = False
    for part in parts[1:]:
        name, separator, value = part.partition("=")
        if not separator or name.strip().lower() != "charset":
            raise InvalidArticle("unsupported_content_type")
        if seen:
            raise InvalidArticle("unsupported_charset")
        seen = True
        charset = value.strip().strip('"').lower()
    if charset not in {"utf-8", "utf8", "us-ascii", "ascii"}:
        raise InvalidArticle("unsupported_charset")
    return "ascii" if charset in {"us-ascii", "ascii"} else "utf-8"


def _has_forbidden_control(value: str) -> bool:
    return any(
        (ord(character) < 32 and character not in "\t\n\r") or ord(character) == 127
        for character in value
    )


def extract_article_html(body: bytes, content_type: str | None) -> ArticleExtraction:
    """Return plain text only; never retain the source HTML in the result."""

    charset = _charset(content_type)
    if not body or len(body) > MAX_BODY_BYTES:
        raise InvalidArticle("invalid_body_size")
    try:
        decoded = body.decode(charset, errors="strict")
    except UnicodeDecodeError as error:
        raise InvalidArticle("invalid_encoding") from error
    if _has_forbidden_control(decoded):
        raise InvalidArticle("invalid_html")
    parser = _ArticleParser()
    try:
        parser.feed(decoded)
        parser.close()
    except InvalidArticle:
        raise
    except (ValueError, AssertionError) as error:
        raise InvalidArticle("invalid_html") from error
    title = parser.title.normalized().replace("\n", " ") or None
    text = next(
        (
            value
            for buffer in (parser.article, parser.main, parser.body, parser.all_text)
            if (value := buffer.normalized())
        ),
        "",
    )
    if not text:
        raise InvalidArticle("empty_content")
    if _has_forbidden_control(title or "") or _has_forbidden_control(text):
        raise InvalidArticle("invalid_html")
    if len(text) > MAX_TEXT_CHARS:
        raise InvalidArticle("output_too_large")
    canonical = json.dumps([title, text], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(b"primary-signal/article-normalization/v1\0" + canonical).hexdigest()
    return ArticleExtraction(
        title=title,
        text=text,
        word_count=len(text.split()),
        normalized_content_hash=digest,
    )
