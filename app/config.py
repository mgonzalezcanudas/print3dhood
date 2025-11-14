from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "print3dhood"
    default_radius_m: int = 250
    max_radius_m: int = 750
    max_buildings: int = 250
    overpass_tile_size_m: int = 300
    overpass_retries: int = 3
    base_thickness_m: float = 0.0075  # per-layer base thickness (7.5 mm)
    green_layer_thickness_m: float = 0.0075
    building_layer_thickness_m: float = 0.0075
    road_groove_depth_m: float = 0.0015
    building_layer_padding_m: float = 2.5
    base_padding_m: float = 5.0
    target_print_diameter_m: float = 0.2
    road_indent_width_m: float = 4.0
    park_indent_shrink_m: float = 1.0
    highlight_peg_depth_m: float = 0.0045
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    overpass_timeout: int = 120
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"
    user_agent: str = Field(
        default="print3dhood/1.0 (contact: example@example.com)",
        description="Identifier for upstream OSM services.",
    )
    default_height_m: float = 10.0
    level_height_m: float = 3.0
    min_height_m: float = 3.0
    highlight_enabled: bool = True
    max_formats: int = 3
    allowed_formats: tuple[str, ...] = ("stl",)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
