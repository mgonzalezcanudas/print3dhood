from typing import List, Sequence

from pydantic import BaseModel, Field, field_validator

from .config import get_settings


class LayerInfo(BaseModel):
    name: str
    thickness_m: float
    description: str


class PolygonPath(BaseModel):
    outer: List[tuple[float, float]]
    holes: List[List[tuple[float, float]]] = []


class LayerPreview(BaseModel):
    name: str
    thickness_m: float
    base_color: str
    feature_color: str
    overlay_color: str | None = None
    description: str
    base_paths: List[PolygonPath]
    feature_paths: List[PolygonPath] = []
    overlay_paths: List[PolygonPath] = []


class GeocodeResult(BaseModel):
    display_name: str
    latitude: float
    longitude: float


class BuildingSummary(BaseModel):
    osm_id: int
    height_m: float
    footprint_area_m2: float
    name: str | None = None


class ModelMetadata(BaseModel):
    building_count: int
    radius_meters: int
    highlighted: bool
    formats: List[str]
    origin: tuple[float, float]
    scale_ratio: float
    layers: List[LayerInfo]
    buildings: List[BuildingSummary]


class ModelRequest(BaseModel):
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    radius_meters: int = Field(..., gt=10, lt=2000)
    highlight_home: bool = True
    formats: List[str] = Field(default_factory=lambda: ["stl"])

    @field_validator("formats")
    @classmethod
    def validate_formats(cls, value: Sequence[str]) -> List[str]:
        settings = get_settings()
        normalized = []
        for fmt in value:
            fmt_lower = fmt.lower()
            if fmt_lower not in settings.allowed_formats:
                raise ValueError(
                    f"Unsupported format '{fmt}'. Choose from {settings.allowed_formats}"
                )
            if fmt_lower not in normalized:
                normalized.append(fmt_lower)
        if not normalized:
            normalized.append(settings.allowed_formats[0])
        if len(normalized) > settings.max_formats:
            raise ValueError(
                f"Select at most {settings.max_formats} formats per request."
            )
        return normalized


class PreviewResponse(BaseModel):
    metadata: ModelMetadata
    previews: List[LayerPreview]
