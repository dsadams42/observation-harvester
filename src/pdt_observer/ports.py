from __future__ import annotations

from typing import Any, Protocol

from pdt_observer.models import PlaceRecord, SourceDocument, SourceSearchResult


class SourceSearcher(Protocol):
    def search_sources(self, query: str, max_results: int) -> list[SourceSearchResult]:
        """Return ranked source candidates for a query."""


class DocumentFetcher(Protocol):
    def fetch_source(self, document_id: str) -> SourceDocument | None:
        """Return a complete source document by ID."""


class SourceRepository(SourceSearcher, DocumentFetcher, Protocol):
    """Search and fetch interface for source documents."""


class Geocoder(Protocol):
    def geocode_place(self, name: str, locality: str, country: str) -> list[PlaceRecord]:
        """Return candidate places matching a name and locality."""


class SpatialGeocoder(Protocol):
    def geocode(self, query: str) -> dict[str, Any] | None:
        """Return the best spatial geocoding result for a free-text query."""

    def reverse(self, latitude: float, longitude: float) -> dict[str, Any] | None:
        """Return a spatial geocoding result for coordinates."""
