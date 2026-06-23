from __future__ import annotations

from pdt_observer.mock_services import MockGeocoder, MockSourceService


def test_mock_search_finds_blue_lantern_source() -> None:
    service = MockSourceService()

    results = service.search_sources("Milltown people_present", max_results=3)

    assert results
    assert results[0].document_id == "milltown-blue-lantern-response"


def test_mock_fetch_unknown_document_returns_none() -> None:
    service = MockSourceService()

    assert service.fetch_source("missing") is None


def test_mock_geocoder_returns_unique_blue_lantern() -> None:
    geocoder = MockGeocoder()

    matches = geocoder.geocode_place("Blue Lantern", "Milltown", "US")

    assert len(matches) == 1
    assert matches[0].place_id == "place-blue-lantern-milltown"


def test_mock_geocoder_can_return_ambiguous_places() -> None:
    geocoder = MockGeocoder()

    matches = geocoder.geocode_place("Harbor Hall", "Milltown", "US")

    assert len(matches) == 2
