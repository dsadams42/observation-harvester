from __future__ import annotations

import math
import re
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from pdt_observer.mock_services import MockGeocoder, MockSourceService
from pdt_observer.models import (
    DayPart,
    InvestigationResult,
    InvestigationRun,
    InvestigationTask,
    ResultStatus,
)
from pdt_observer.ports import DocumentFetcher, Geocoder
from pdt_observer.time_context import day_part_for_local_time


class ValidationCode(StrEnum):
    REQUIRED_FIELD_MISSING = "required_field_missing"
    DOCUMENT_NOT_FOUND = "document_not_found"
    SOURCE_URL_MISMATCH = "source_url_mismatch"
    QUOTE_NOT_FOUND = "quote_not_found"
    COUNT_NOT_IN_QUOTE = "count_not_in_quote"
    PLACE_ID_NOT_FOUND = "place_id_not_found"
    COORDINATE_MISMATCH = "coordinate_mismatch"
    LOCALITY_COUNTRY_MISMATCH = "locality_country_mismatch"
    OBSERVATION_TYPE_MISMATCH = "observation_type_mismatch"
    TIME_TEXT_NOT_IN_QUOTE = "time_text_not_in_quote"
    TIME_CONTEXT_WITHOUT_SOURCE_TEXT = "time_context_without_source_text"
    TIME_CONTEXT_DAY_PART_MISMATCH = "time_context_day_part_mismatch"


class ObservationValidationError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ValidationCode
    message: str


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool
    errors: tuple[ObservationValidationError, ...] = ()


class ObservationValidationException(ValueError):
    def __init__(self, errors: Sequence[ObservationValidationError]) -> None:
        self.errors = tuple(errors)
        message = "; ".join(error.message for error in self.errors)
        super().__init__(message)


def _error(code: ValidationCode, message: str) -> ObservationValidationError:
    return ObservationValidationError(code=code, message=message)


def _number_occurs_in_text(number: int, text: str) -> bool:
    return re.search(rf"(?<!\d){number}(?!\d)", text) is not None


def validate_result(
    result: InvestigationResult,
    task: InvestigationTask,
    document_fetcher: DocumentFetcher,
    geocoder: Geocoder,
) -> ValidationReport:
    if result.status != ResultStatus.ACCEPTED:
        return ValidationReport(valid=True)

    errors: list[ObservationValidationError] = []
    if result.count is None:
        errors.append(_error(ValidationCode.REQUIRED_FIELD_MISSING, "accepted result lacks count"))
    if result.evidence is None:
        errors.append(
            _error(ValidationCode.REQUIRED_FIELD_MISSING, "accepted result lacks evidence")
        )
    if result.georeference is None:
        errors.append(
            _error(ValidationCode.REQUIRED_FIELD_MISSING, "accepted result lacks georeference")
        )
    if result.observation_type != task.observation_type:
        errors.append(
            _error(
                ValidationCode.OBSERVATION_TYPE_MISMATCH,
                "accepted result observation_type does not match the task",
            )
        )

    if result.evidence is None or result.georeference is None or result.count is None:
        return ValidationReport(valid=False, errors=tuple(errors))

    time_context = result.time_context
    if time_context is not None:
        context_has_claim = (
            time_context.observed_time_local is not None
            or time_context.day_part != DayPart.UNKNOWN
            or time_context.timezone is not None
        )
        if context_has_claim and result.observed_time_text is None:
            errors.append(
                _error(
                    ValidationCode.TIME_CONTEXT_WITHOUT_SOURCE_TEXT,
                    "time_context claims require observed_time_text copied from the source",
                )
            )
        if time_context.observed_time_local is not None:
            expected_day_part = day_part_for_local_time(time_context.observed_time_local)
            if (
                time_context.day_part != DayPart.UNKNOWN
                and time_context.day_part != expected_day_part
            ):
                errors.append(
                    _error(
                        ValidationCode.TIME_CONTEXT_DAY_PART_MISMATCH,
                        "time_context day_part does not match observed_time_local",
                    )
                )

    document = document_fetcher.fetch_source(result.evidence.document_id)
    if document is None:
        errors.append(
            _error(
                ValidationCode.DOCUMENT_NOT_FOUND,
                f"document {result.evidence.document_id!r} does not exist",
            )
        )
    else:
        if result.evidence.source_url != document.source_url:
            errors.append(
                _error(
                    ValidationCode.SOURCE_URL_MISMATCH,
                    "evidence source_url does not match the stored document",
                )
            )
        if result.evidence.supporting_quote not in document.text:
            errors.append(
                _error(
                    ValidationCode.QUOTE_NOT_FOUND,
                    "supporting quote is not an exact substring of the source text",
                )
            )
        if not _number_occurs_in_text(result.count, result.evidence.supporting_quote):
            errors.append(
                _error(
                    ValidationCode.COUNT_NOT_IN_QUOTE,
                    "accepted count does not occur in the supporting quote",
                )
            )
        if (
            result.observed_time_text is not None
            and result.observed_time_text not in result.evidence.supporting_quote
        ):
            errors.append(
                _error(
                    ValidationCode.TIME_TEXT_NOT_IN_QUOTE,
                    "observed_time_text is not an exact substring of the supporting quote",
                )
            )

    matches = geocoder.geocode_place(
        result.georeference.place_name,
        task.locality,
        task.country,
    )
    selected_place = next(
        (place for place in matches if place.place_id == result.georeference.place_id),
        None,
    )
    if selected_place is None:
        errors.append(
            _error(
                ValidationCode.PLACE_ID_NOT_FOUND,
                "selected place_id was not returned by geocoding the place and locality",
            )
        )
    else:
        if not (
            math.isclose(result.georeference.latitude, selected_place.latitude, abs_tol=1e-9)
            and math.isclose(result.georeference.longitude, selected_place.longitude, abs_tol=1e-9)
        ):
            errors.append(
                _error(
                    ValidationCode.COORDINATE_MISMATCH,
                    "returned coordinates do not match the selected place record",
                )
            )
        if (
            selected_place.locality.casefold() != task.locality.casefold()
            or selected_place.country.casefold() != task.country.casefold()
            or result.georeference.locality.casefold() != selected_place.locality.casefold()
            or result.georeference.country.casefold() != selected_place.country.casefold()
        ):
            errors.append(
                _error(
                    ValidationCode.LOCALITY_COUNTRY_MISMATCH,
                    "selected place or returned georeference has a different locality or country",
                )
            )

    return ValidationReport(valid=not errors, errors=tuple(errors))


def raise_for_invalid(report: ValidationReport) -> None:
    if not report.valid:
        raise ObservationValidationException(report.errors)


def validate_run(run: InvestigationRun) -> ValidationReport:
    source_service = MockSourceService(run.source_bundle.documents)
    geocoder = MockGeocoder(run.source_bundle.places)
    return validate_result(run.candidate.result, run.task, source_service, geocoder)
