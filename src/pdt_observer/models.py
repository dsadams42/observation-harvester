from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ObservationType(StrEnum):
    PEOPLE_PRESENT = "people_present"


class ResultStatus(StrEnum):
    ACCEPTED = "accepted"
    REVIEW = "review"
    NOT_FOUND = "not_found"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InvestigationTask(StrictModel):
    task_id: str = Field(min_length=1)
    locality: str = Field(min_length=1)
    country: str = Field(min_length=2)
    observation_type: ObservationType
    maximum_agent_turns: int = Field(default=6, ge=1, le=20)


class Evidence(StrictModel):
    document_id: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    supporting_quote: str = Field(min_length=1)


class GeoReference(StrictModel):
    place_id: str = Field(min_length=1)
    place_name: str = Field(min_length=1)
    locality: str = Field(min_length=1)
    country: str = Field(min_length=2)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    method: str = Field(min_length=1)


class InvestigationResult(StrictModel):
    status: ResultStatus
    count: int | None = Field(default=None, ge=0)
    observation_type: ObservationType | None = None
    place_name: str | None = None
    observed_time_text: str | None = None
    evidence: Evidence | None = None
    georeference: GeoReference | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def accepted_results_are_complete(self) -> Self:
        if self.status != ResultStatus.ACCEPTED:
            return self

        missing: list[str] = []
        if self.count is None:
            missing.append("count")
        if self.observation_type is None:
            missing.append("observation_type")
        if self.place_name is None:
            missing.append("place_name")
        if self.evidence is None:
            missing.append("evidence")
        if self.georeference is None:
            missing.append("georeference")
        if missing:
            fields = ", ".join(missing)
            raise ValueError(f"accepted result missing required field(s): {fields}")
        return self


class SourceSearchResult(StrictModel):
    document_id: str
    title: str
    source_url: str
    snippet: str
    score: int


class SourceDocument(StrictModel):
    document_id: str
    title: str
    source_url: str
    locality: str
    country: str
    text: str
    tags: tuple[str, ...] = ()


class PlaceRecord(StrictModel):
    place_id: str
    name: str
    locality: str
    country: str
    latitude: float
    longitude: float
    method: str
