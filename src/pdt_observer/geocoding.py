from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from pdt_observer.storage import write_json_file


def geocode_cache_path(root: Path) -> Path:
    return root / "geocode_cache" / "nominatim.json"


class NominatimGeocoder:
    def __init__(self, root: Path, *, min_interval_seconds: float = 1.0) -> None:
        self.root = root
        self.min_interval_seconds = min_interval_seconds
        self._lock = threading.Lock()
        self._last_request_at = 0.0

    def _load_cache(self) -> dict[str, dict[str, Any] | None]:
        path = geocode_cache_path(self.root)
        if not path.is_file():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}

    def _write_cache(self, cache: dict[str, dict[str, Any] | None]) -> None:
        write_json_file(geocode_cache_path(self.root), cache, sort_keys=True)

    def __call__(self, query: str) -> dict[str, Any] | None:
        return self.geocode(query)

    def geocode(self, query: str) -> dict[str, Any] | None:
        with self._lock:
            cache = self._load_cache()
            cached = cache.get(query)
            if isinstance(cached, dict) and cached.get("cache_version") == 2:
                return cached
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_interval_seconds:
                time.sleep(self.min_interval_seconds - elapsed)
            parameters = {
                "q": query,
                "format": "jsonv2",
                "limit": "5",
                "addressdetails": "1",
            }
            country_match = re.search(r"(?:^|,\s*)([A-Za-z]{2})\s*$", query)
            if country_match is not None:
                parameters["countrycodes"] = country_match.group(1).lower()
            params = urllib.parse.urlencode(parameters)
            request = urllib.request.Request(
                f"https://nominatim.openstreetmap.org/search?{params}",
                headers={
                    "User-Agent": "pdt-observer-local-app/0.1 (user-triggered geometry review)"
                },
            )
            self._last_request_at = time.monotonic()
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            candidates = []
            if isinstance(payload, list):
                for candidate in payload:
                    if (
                        not isinstance(candidate, dict)
                        or "lat" not in candidate
                        or "lon" not in candidate
                    ):
                        continue
                    candidates.append(
                        {
                            "display_name": candidate.get("display_name", ""),
                            "latitude": float(candidate["lat"]),
                            "longitude": float(candidate["lon"]),
                            "provider": "nominatim",
                            "query": query,
                            "category": candidate.get("category"),
                            "type": candidate.get("type"),
                            "name": candidate.get("name"),
                            "importance": candidate.get("importance"),
                            "address": candidate.get("address", {}),
                        }
                    )
            first = candidates[0] if candidates else None
            result = (
                {
                    **first,
                    "candidates": candidates,
                    "cache_version": 2,
                }
                if first is not None
                else None
            )
            cache[query] = result
            self._write_cache(cache)
            return result

    def reverse(self, latitude: float, longitude: float) -> dict[str, Any] | None:
        cache_key = f"reverse:{latitude:.7f},{longitude:.7f}"
        with self._lock:
            cache = self._load_cache()
            cached = cache.get(cache_key)
            if isinstance(cached, dict) and cached.get("cache_version") == 2:
                return cached
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_interval_seconds:
                time.sleep(self.min_interval_seconds - elapsed)
            parameters = urllib.parse.urlencode(
                {
                    "lat": f"{latitude:.7f}",
                    "lon": f"{longitude:.7f}",
                    "format": "jsonv2",
                    "addressdetails": "1",
                    "zoom": "18",
                }
            )
            request = urllib.request.Request(
                f"https://nominatim.openstreetmap.org/reverse?{parameters}",
                headers={
                    "User-Agent": "pdt-observer-local-app/0.1 "
                    "(user-triggered coordinate review)"
                },
            )
            self._last_request_at = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    cache[cache_key] = None
                    self._write_cache(cache)
                    return None
                raise
            if not isinstance(payload, dict) or "lat" not in payload or "lon" not in payload:
                result = None
            else:
                result = {
                    "display_name": payload.get("display_name", ""),
                    "latitude": float(payload["lat"]),
                    "longitude": float(payload["lon"]),
                    "provider": "nominatim-reverse",
                    "query": cache_key,
                    "category": payload.get("category"),
                    "type": payload.get("type"),
                    "name": payload.get("name"),
                    "importance": payload.get("importance"),
                    "address": payload.get("address", {}),
                    "cache_version": 2,
                }
            cache[cache_key] = result
            self._write_cache(cache)
            return result
