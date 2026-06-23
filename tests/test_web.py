from __future__ import annotations

import urllib.request

import pytest

from pdt_observer.web import (
    ContentTooLargeError,
    DirectWebFetcher,
    canonicalize_url,
    extract_feed_or_sitemap_urls,
    extract_html_text,
)


class _FakeHeaders:
    def __init__(self, content_type: str) -> None:
        self._content_type = content_type

    def get_content_type(self) -> str:
        return self._content_type


class _FakeResponse:
    status = 200

    def __init__(self, body: bytes, content_type: str) -> None:
        self._body = body
        self.headers = _FakeHeaders(content_type)

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self._body[:size]


def test_canonicalize_url_dedupes_fragment_and_query_order() -> None:
    assert (
        canonicalize_url("HTTPS://Example.COM/path?b=2&a=1#fragment")
        == "https://example.com/path?a=1&b=2"
    )


def test_extract_html_text_ignores_script_and_preserves_title() -> None:
    title, text = extract_html_text(
        "<html><head><title>Incident</title><script>ignore()</script></head>"
        "<body><h1>Blue Lantern</h1><p>Officials said 17 people were inside.</p></body></html>"
    )

    assert title == "Incident"
    assert "Blue Lantern" in text
    assert "ignore()" not in text


def test_extract_feed_and_sitemap_urls() -> None:
    urls = extract_feed_or_sitemap_urls(
        """
        <urlset>
          <url><loc>https://Example.com/b?z=1&amp;a=2</loc></url>
          <url><loc>https://example.com/a</loc></url>
        </urlset>
        """
    )

    assert urls == ("https://example.com/b?a=2&z=1", "https://example.com/a")


def test_direct_fetcher_uses_get_and_extracts_html(monkeypatch) -> None:
    body = b"<html><title>Story</title><body><p>Officials said 17 people were inside.</p></body>"

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        assert request.get_method() == "GET"
        assert timeout == 5
        return _FakeResponse(body, "text/html")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(DirectWebFetcher, "_ensure_robots_allowed", lambda self, url: None)

    result = DirectWebFetcher(timeout_seconds=5, min_domain_interval_seconds=0).fetch(
        "https://example.com/story#fragment"
    )

    assert result.canonical_url == "https://example.com/story"
    assert result.title == "Story"
    assert "17 people" in result.text
    assert result.content_sha256


def test_direct_fetcher_rejects_oversized_response(monkeypatch) -> None:
    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        return _FakeResponse(b"abcdef", "text/plain")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(DirectWebFetcher, "_ensure_robots_allowed", lambda self, url: None)

    with pytest.raises(ContentTooLargeError):
        DirectWebFetcher(max_bytes=3, min_domain_interval_seconds=0).fetch("https://example.com")
