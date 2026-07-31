from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from pdt_observer.models import GeometryPoint, GeometryStatus


class HarvestRunRequest(BaseModel):
    country: str = Field(min_length=2)
    locality: str | None = None
    profiles: str = "schools"
    profile: str | None = None
    target: int = Field(default=20, ge=1)
    run_id: str | None = None
    geographer_plan_path: str | None = None


class HarvestBatchRunRequest(BaseModel):
    country: str = Field(min_length=2)
    locality: str | None = None
    profiles: str = "schools"
    target: int = Field(default=20, ge=1)
    batch_id: str | None = None
    geographer_plan_path: str | None = None


class GeographerPlanRequest(BaseModel):
    country: str = Field(min_length=2)
    locality: str | None = None
    profiles: str = "schools"
    profile: str | None = None
    localities: tuple[str, ...] = ()
    facility_types: tuple[str, ...] = ()
    mode: str = Field(default="single", pattern=r"^(single|batch|campaign)$")


class HarvestCampaignRunRequest(BaseModel):
    country: str = Field(min_length=2)
    localities: tuple[str, ...] = ()
    facility_types: tuple[str, ...] = Field(min_length=1)
    target: int = Field(default=20, ge=1)
    campaign_id: str | None = None
    geographer_plan_path: str | None = None


class PromoteLeadRequest(BaseModel):
    index: int = Field(ge=0)
    task_id: str | None = None


class GeometryGeocodeRequest(BaseModel):
    item_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    allow_address_retry: bool = False
    conversation_id: str | None = None


class GeometryResearchRequest(BaseModel):
    item_id: str = Field(min_length=1)
    conversation_id: str | None = None


class GeometryCoordinatePreviewRequest(BaseModel):
    item_id: str = Field(min_length=1)
    coordinate_text: str = Field(min_length=1)


class GeometryGeocodeAllRequest(BaseModel):
    items: tuple[GeometryGeocodeRequest, ...] = Field(min_length=1)


class GeometrySaveRequest(BaseModel):
    item_id: str = Field(min_length=1)
    geocode_query: str = Field(min_length=1)
    point: GeometryPoint | None = None
    polygon_geojson: dict[str, Any] | None = None
    geometry_status: GeometryStatus = GeometryStatus.NEEDS_REVIEW
    geocode_result: dict[str, Any] | None = None
    spatial_validation: dict[str, Any] | None = None
    review_notes: str | None = None
    conversation_id: str | None = None


class SampleSetCreateRequest(BaseModel):
    run_id: str = Field(min_length=1)
    sample_set_id: str | None = None


class SampleSetGapFillRequest(BaseModel):
    coverage_id: str | None = None
