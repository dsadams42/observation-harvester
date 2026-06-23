from __future__ import annotations

from pdt_observer.agent import build_result_from_document, run_offline_demo
from pdt_observer.mock_data import DEFAULT_MILLTOWN_TASK
from pdt_observer.mock_services import MockGeocoder, MockSourceService
from pdt_observer.models import Evidence, GeoReference, InvestigationResult
from pdt_observer.validation import ValidationCode, validate_result


def _services() -> tuple[MockSourceService, MockGeocoder]:
    return MockSourceService(), MockGeocoder()


def _successful_result() -> tuple[InvestigationResult, MockSourceService, MockGeocoder]:
    source_service, geocoder = _services()
    return run_offline_demo(), source_service, geocoder


def _has_code(result: InvestigationResult, code: ValidationCode) -> bool:
    source_service, geocoder = _services()
    report = validate_result(result, DEFAULT_MILLTOWN_TASK, source_service, geocoder)
    return any(error.code == code for error in report.errors)


def test_successful_17_person_observation_validates() -> None:
    result, source_service, geocoder = _successful_result()

    report = validate_result(result, DEFAULT_MILLTOWN_TASK, source_service, geocoder)

    assert report.valid
    assert result.status == "accepted"
    assert result.count == 17
    assert result.observation_type == "people_present"
    assert result.place_name == "Blue Lantern"
    assert result.evidence is not None
    assert result.georeference is not None
    assert result.georeference.latitude == 41.24831
    assert result.georeference.longitude == -72.67344


def test_supporting_quote_is_exact_source_text() -> None:
    result, source_service, _geocoder = _successful_result()

    assert result.evidence is not None
    document = source_service.fetch_source(result.evidence.document_id)

    assert document is not None
    assert result.evidence.supporting_quote in document.text


def test_validation_rejects_quote_not_found_in_source() -> None:
    result, _source_service, _geocoder = _successful_result()
    assert result.evidence is not None

    bad = result.model_copy(
        deep=True,
        update={
            "evidence": result.evidence.model_copy(
                update={"supporting_quote": "Officials said 17 people were somewhere else."}
            )
        },
    )

    assert _has_code(bad, ValidationCode.QUOTE_NOT_FOUND)


def test_validation_rejects_count_absent_from_quote() -> None:
    result, _source_service, _geocoder = _successful_result()
    assert result.evidence is not None

    bad = result.model_copy(
        deep=True,
        update={
            "evidence": result.evidence.model_copy(
                update={"supporting_quote": "crews arrived at approximately 9:10 p.m."}
            )
        },
    )

    assert _has_code(bad, ValidationCode.COUNT_NOT_IN_QUOTE)


def test_validation_rejects_unknown_place_id() -> None:
    result, _source_service, _geocoder = _successful_result()
    assert result.georeference is not None

    bad = result.model_copy(
        deep=True,
        update={
            "georeference": result.georeference.model_copy(update={"place_id": "missing-place"})
        },
    )

    assert _has_code(bad, ValidationCode.PLACE_ID_NOT_FOUND)


def test_validation_rejects_altered_coordinates() -> None:
    result, _source_service, _geocoder = _successful_result()
    assert result.georeference is not None

    bad = result.model_copy(
        deep=True,
        update={
            "georeference": result.georeference.model_copy(update={"latitude": 42.0})
        },
    )

    assert _has_code(bad, ValidationCode.COORDINATE_MISMATCH)


def test_ambiguous_venue_returns_review() -> None:
    source_service, geocoder = _services()
    document = source_service.fetch_source("milltown-harbor-hall-crowd")

    assert document is not None
    result = build_result_from_document(DEFAULT_MILLTOWN_TASK, document, geocoder)

    assert result.status == "review"
    assert result.count == 22
    assert result.evidence is not None
    assert result.georeference is None


def test_document_without_qualifying_count_returns_not_found() -> None:
    source_service, geocoder = _services()
    document = source_service.fetch_source("milltown-profile-no-count")

    assert document is not None
    result = build_result_from_document(DEFAULT_MILLTOWN_TASK, document, geocoder)

    assert result.status == "not_found"


def test_other_number_types_do_not_become_people_present_observations() -> None:
    source_service, geocoder = _services()
    document = source_service.fetch_source("milltown-number-traps")

    assert document is not None
    result = build_result_from_document(DEFAULT_MILLTOWN_TASK, document, geocoder)

    assert result.status == "not_found"


def test_validation_rejects_document_url_mismatch() -> None:
    result, _source_service, _geocoder = _successful_result()
    assert result.evidence is not None

    bad_evidence = Evidence(
        document_id=result.evidence.document_id,
        source_url="https://news.example.invalid/altered",
        supporting_quote=result.evidence.supporting_quote,
    )
    bad = result.model_copy(deep=True, update={"evidence": bad_evidence})

    assert _has_code(bad, ValidationCode.SOURCE_URL_MISMATCH)


def test_validation_rejects_locality_country_mismatch_from_constructed_result() -> None:
    result, _source_service, _geocoder = _successful_result()
    assert result.georeference is not None

    bad_geo = GeoReference(
        place_id=result.georeference.place_id,
        place_name=result.georeference.place_name,
        locality="Other Town",
        country=result.georeference.country,
        latitude=result.georeference.latitude,
        longitude=result.georeference.longitude,
        method=result.georeference.method,
    )
    bad = result.model_copy(deep=True, update={"georeference": bad_geo})

    assert _has_code(bad, ValidationCode.LOCALITY_COUNTRY_MISMATCH)
