from __future__ import annotations

import re

from pdt_observer.mock_data import MOCK_DOCUMENTS, MOCK_PLACES
from pdt_observer.models import PlaceRecord, SourceDocument, SourceSearchResult

_WORD_RE = re.compile(r"[a-z0-9]+")
_BUSINESS_SUFFIX_RE = re.compile(r"\s+(restaurant|venue|bar|store)\Z", re.IGNORECASE)


def _tokens(value: str) -> set[str]:
    return set(_WORD_RE.findall(value.lower().replace("_", " ")))


def _normalize_place_name(value: str) -> str:
    compact = " ".join(value.strip().split())
    compact = _BUSINESS_SUFFIX_RE.sub("", compact)
    return compact.casefold()


class MockSourceService:
    def __init__(self, documents: tuple[SourceDocument, ...] = MOCK_DOCUMENTS) -> None:
        self._documents = {document.document_id: document for document in documents}

    def search_sources(self, query: str, max_results: int) -> list[SourceSearchResult]:
        if max_results < 1:
            return []

        query_tokens = _tokens(query)
        ranked: list[SourceSearchResult] = []
        for document in self._documents.values():
            haystack = " ".join(
                (
                    document.title,
                    document.locality,
                    document.country,
                    document.text,
                    " ".join(document.tags),
                )
            )
            score = len(query_tokens & _tokens(haystack))
            if score == 0:
                continue
            ranked.append(
                SourceSearchResult(
                    document_id=document.document_id,
                    title=document.title,
                    source_url=document.source_url,
                    snippet=document.text[:180],
                    score=score,
                )
            )

        ranked.sort(key=lambda result: (-result.score, result.document_id))
        return ranked[:max_results]

    def fetch_source(self, document_id: str) -> SourceDocument | None:
        return self._documents.get(document_id)


class MockGeocoder:
    def __init__(self, places: tuple[PlaceRecord, ...] = MOCK_PLACES) -> None:
        self._places = places

    def geocode_place(self, name: str, locality: str, country: str) -> list[PlaceRecord]:
        normalized_name = _normalize_place_name(name)
        normalized_locality = locality.casefold()
        normalized_country = country.casefold()
        return [
            place
            for place in self._places
            if _normalize_place_name(place.name) == normalized_name
            and place.locality.casefold() == normalized_locality
            and place.country.casefold() == normalized_country
        ]
