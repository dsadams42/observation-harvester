from __future__ import annotations

import hashlib
import time
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from html.parser import HTMLParser

from pdt_observer.models import DirectFetchResult

ALLOWED_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/rss+xml",
    "application/atom+xml",
    "application/xml",
    "text/xml",
)


class DirectFetchError(RuntimeError):
    pass


class RobotsBlockedError(DirectFetchError):
    pass


class ContentTypeRejectedError(DirectFetchError):
    pass


class ContentTooLargeError(DirectFetchError):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self._in_title = False
        self._blocked_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._blocked_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in {"p", "br", "div", "li", "h1", "h2", "h3", "article", "section"}:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._blocked_depth:
            self._blocked_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in {"p", "div", "li", "h1", "h2", "h3", "article", "section"}:
            self._parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._blocked_depth:
            return
        if self._in_title:
            self.title += data
        self._parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self._parts).split())


def canonicalize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    query_items = sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query = urllib.parse.urlencode(query_items)
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def extract_html_text(html: str) -> tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(html)
    return " ".join(parser.title.split()), parser.text()


def extract_feed_or_sitemap_urls(xml_text: str) -> tuple[str, ...]:
    root = ET.fromstring(xml_text)
    urls: list[str] = []
    for element in root.iter():
        tag = element.tag.rsplit("}", maxsplit=1)[-1].casefold()
        text = "" if element.text is None else element.text.strip()
        if tag in {"loc", "link"} and text.startswith(("http://", "https://")):
            urls.append(canonicalize_url(text))
        href = element.attrib.get("href")
        if tag == "link" and href and href.startswith(("http://", "https://")):
            urls.append(canonicalize_url(href))
    return tuple(dict.fromkeys(urls))


class DomainRateLimiter:
    def __init__(
        self,
        min_interval_seconds: float,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval_seconds = min_interval_seconds
        self._sleeper = sleeper
        self._last_request_at: dict[str, float] = {}

    def wait(self, url: str) -> None:
        if self._min_interval_seconds <= 0:
            return
        domain = urllib.parse.urlsplit(url).netloc.lower()
        now = time.monotonic()
        last = self._last_request_at.get(domain)
        if last is not None:
            delay = self._min_interval_seconds - (now - last)
            if delay > 0:
                self._sleeper(delay)
                now = time.monotonic()
        self._last_request_at[domain] = now


class DirectWebFetcher:
    def __init__(
        self,
        *,
        user_agent: str = "pdt-observer/0.1",
        timeout_seconds: float = 10,
        max_bytes: int = 1_000_000,
        min_domain_interval_seconds: float = 1.0,
    ) -> None:
        self._user_agent = user_agent
        self._timeout_seconds = timeout_seconds
        self._max_bytes = max_bytes
        self._limiter = DomainRateLimiter(min_domain_interval_seconds)

    def fetch(self, url: str) -> DirectFetchResult:
        canonical_url = canonicalize_url(url)
        self._ensure_robots_allowed(canonical_url)
        self._limiter.wait(canonical_url)

        request = urllib.request.Request(
            canonical_url,
            method="GET",
            headers={"User-Agent": self._user_agent},
        )
        with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
            content_type = response.headers.get_content_type()
            if content_type not in ALLOWED_CONTENT_TYPES:
                raise ContentTypeRejectedError(f"unsupported content type: {content_type}")
            raw = response.read(self._max_bytes + 1)
            if len(raw) > self._max_bytes:
                raise ContentTooLargeError(f"response exceeds {self._max_bytes} bytes")
            status_code = response.status

        text = raw.decode("utf-8", errors="replace")
        title = ""
        discovered_urls: tuple[str, ...] = ()
        if content_type in {"text/html", "application/xhtml+xml"}:
            title, text = extract_html_text(text)
        elif content_type in {
            "application/rss+xml",
            "application/atom+xml",
            "application/xml",
            "text/xml",
        }:
            discovered_urls = extract_feed_or_sitemap_urls(text)

        now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        digest = hashlib.sha256(raw).hexdigest()
        return DirectFetchResult(
            url=url,
            canonical_url=canonical_url,
            source_url=canonical_url,
            title=title,
            text=text,
            content_type=content_type,
            status_code=status_code,
            content_sha256=digest,
            fetched_at=now,
            discovered_urls=discovered_urls,
        )

    def _ensure_robots_allowed(self, url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            raise DirectFetchError("only http and https URLs are supported")
        robots_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
        parser = urllib.robotparser.RobotFileParser(robots_url)
        parser.read()
        if not parser.can_fetch(self._user_agent, url):
            raise RobotsBlockedError(f"robots.txt disallows fetching {url}")
