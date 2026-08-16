from __future__ import annotations

import inspect
import json
import urllib.request
from collections.abc import Iterator
from pathlib import Path

from pdt_observer import geocoding, geometry
from pdt_observer.geocoding import NominatimGeocoder, geocode_cache_path


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_geometry_module_does_not_own_nominatim_service_code() -> None:
    source = inspect.getsource(geometry)

    assert not hasattr(geometry, "NominatimGeocoder")
    assert "urlopen" not in source
    assert "urllib.request" not in source


def test_nominatim_geocoder_caches_forward_and_reverse(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payloads: Iterator[object] = iter(
        [
            [
                {
                    "display_name": "Example Warehouse, Tennessee, US",
                    "lat": "36.1000000",
                    "lon": "-86.7000000",
                    "category": "building",
                    "type": "industrial",
                    "name": "Example Warehouse",
                    "importance": 0.7,
                    "address": {"country_code": "us", "state": "Tennessee"},
                }
            ],
            {
                "display_name": "Example Warehouse, Tennessee, US",
                "lat": "36.1000000",
                "lon": "-86.7000000",
                "category": "building",
                "type": "industrial",
                "name": "Example Warehouse",
                "importance": 0.7,
                "address": {"country_code": "us", "state": "Tennessee"},
            },
        ]
    )
    requested_urls: list[str] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        requested_urls.append(request.full_url)
        assert timeout == 10
        return _FakeResponse(next(payloads))

    monkeypatch.setattr(geocoding.urllib.request, "urlopen", fake_urlopen)
    geocoder = NominatimGeocoder(tmp_path, min_interval_seconds=0)

    forward = geocoder.geocode("Example Warehouse, Tennessee, US")
    cached_forward = geocoder.geocode("Example Warehouse, Tennessee, US")
    reverse = geocoder.reverse(36.1, -86.7)
    cached_reverse = geocoder.reverse(36.1, -86.7)

    assert forward == cached_forward
    assert reverse == cached_reverse
    assert forward is not None
    assert forward["provider"] == "nominatim"
    assert reverse is not None
    assert reverse["provider"] == "nominatim-reverse"
    assert len(requested_urls) == 2
    assert "search?" in requested_urls[0]
    assert "reverse?" in requested_urls[1]
    assert geocode_cache_path(tmp_path).is_file()
    assert list(geocode_cache_path(tmp_path).parent.glob("*.tmp")) == []
