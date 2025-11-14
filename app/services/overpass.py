from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Iterable

import httpx
from pyproj import Transformer
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiPolygon,
    Point,
    Polygon,
)
from shapely.validation import make_valid

from ..config import get_settings

_transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
_inverse_transformer = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)


@dataclass
class BuildingFootprint:
    osm_id: int
    polygon_projected: Polygon
    polygon_wgs84: Polygon
    height_m: float
    name: str | None
    tags: dict[str, Any]

    @property
    def area_m2(self) -> float:
        return float(self.polygon_projected.area)


@dataclass
class RoadFeature:
    osm_id: int
    line_projected: LineString
    tags: dict[str, Any]


@dataclass
class ParkFeature:
    osm_id: int
    polygon_projected: Polygon
    tags: dict[str, Any]


@dataclass
class WaterFeature:
    osm_id: int
    polygon_projected: Polygon
    tags: dict[str, Any]


@dataclass
class OverpassError(Exception):
    message: str
    status_code: int = 503

    def __str__(self) -> str:  # pragma: no cover
        return self.message


async def fetch_environment(
    latitude: float, longitude: float, radius_m: int
) -> tuple[
    list[BuildingFootprint],
    list[RoadFeature],
    list[ParkFeature],
    list[WaterFeature],
]:
    settings = get_settings()
    center_x, center_y = _project((longitude, latitude))
    tiles = _build_tiles(longitude, latitude, radius_m, settings.overpass_tile_size_m)
    footprints: dict[int, BuildingFootprint] = {}
    roads: dict[int, RoadFeature] = {}
    parks: dict[int, ParkFeature] = {}
    waters: dict[int, WaterFeature] = {}
    async with httpx.AsyncClient(
        headers={"User-Agent": settings.user_agent},
        timeout=settings.overpass_timeout + 30,
    ) as client:
        for south, west, north, east in tiles:
            overpass_query = _bbox_query(south, west, north, east, settings.overpass_timeout)
            payload = await _execute_overpass(client, overpass_query, settings)
            parsed_buildings, parsed_roads, parsed_parks, parsed_waters = _parse_payload(
                payload
            )
            for footprint in parsed_buildings:
                footprints.setdefault(footprint.osm_id, footprint)
            for road in parsed_roads:
                roads.setdefault(road.osm_id, road)
            for park in parsed_parks:
                parks.setdefault(park.osm_id, park)
            for water in parsed_waters:
                waters.setdefault(water.osm_id, water)

    if not footprints:
        return [], [], [], []

    circle = Point(center_x, center_y).buffer(radius_m)
    filtered_buildings = [
        footprint
        for footprint in footprints.values()
        if footprint.polygon_projected.intersects(circle)
    ]
    filtered_buildings.sort(key=lambda footprint: footprint.area_m2, reverse=True)

    filtered_roads = [
        road
        for road in roads.values()
        if road.line_projected.intersects(circle)
    ]
    filtered_parks = [
        park
        for park in parks.values()
        if park.polygon_projected.intersects(circle)
    ]
    filtered_waters = [
        water
        for water in waters.values()
        if water.polygon_projected.intersects(circle)
    ]
    return (
        filtered_buildings[: settings.max_buildings],
        filtered_roads,
        filtered_parks,
        filtered_waters,
    )


def _bbox_query(south: float, west: float, north: float, east: float, timeout: int) -> str:
    return f"""
        [out:json][timeout:{timeout}];
        (
          way["building"]({south},{west},{north},{east});
          way["highway"]({south},{west},{north},{east});
          way["leisure"="park"]({south},{west},{north},{east});
          way["landuse"="grass"]({south},{west},{north},{east});
          way["landuse"="recreation_ground"]({south},{west},{north},{east});
          way["landuse"="meadow"]({south},{west},{north},{east});
          way["natural"="water"]({south},{west},{north},{east});
          way["waterway"="riverbank"]({south},{west},{north},{east});
          way["water"="lake"]({south},{west},{north},{east});
          way["landuse"="reservoir"]({south},{west},{north},{east});
        );
        (._;>;);
        out body;
    """


async def _execute_overpass(
    client: httpx.AsyncClient, query: str, settings
) -> dict[str, Any]:
    for attempt in range(settings.overpass_retries):
        try:
            response = await client.post(settings.overpass_url, data={"data": query})
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (429, 504) and attempt < settings.overpass_retries - 1:
                await asyncio.sleep(1 + attempt)
                continue
            if status == 429:
                raise OverpassError(
                    "Overpass rate limit hit (HTTP 429). Please wait a few seconds or reduce the radius before retrying.",
                    status_code=503,
                ) from exc
            if status == 504:
                raise OverpassError(
                    "Overpass timed out while retrieving buildings (HTTP 504). Try a smaller radius or retry shortly.",
                    status_code=503,
                ) from exc
            raise OverpassError(
                f"Overpass request failed with status {status}.", status_code=503
            ) from exc
        except httpx.RequestError as exc:  # pragma: no cover
            raise OverpassError("Unable to reach the Overpass API.") from exc
    raise OverpassError("Overpass query failed after multiple retries.")


def _parse_payload(
    payload: dict[str, Any]
) -> tuple[
    Iterable[BuildingFootprint],
    Iterable[RoadFeature],
    Iterable[ParkFeature],
    Iterable[WaterFeature],
]:
    elements = payload.get("elements", [])
    node_index: dict[int, tuple[float, float]] = {
        element["id"]: (element["lon"], element["lat"])
        for element in elements
        if element.get("type") == "node"
    }

    buildings: list[BuildingFootprint] = []
    roads: list[RoadFeature] = []
    parks: list[ParkFeature] = []
    waters: list[WaterFeature] = []
    for element in elements:
        if element.get("type") != "way":
            continue
        node_ids = element.get("nodes", [])
        coords = [_lonlat_from_node(node_index, node_id) for node_id in node_ids]
        coords = [coord for coord in coords if coord]
        if len(coords) < 3:
            continue
        tags = element.get("tags", {}) or {}
        if "building" in tags:
            building_coords = coords[:]
            if building_coords[0] != building_coords[-1]:
                building_coords.append(building_coords[0])
            polygon_wgs84 = _build_polygon(building_coords, tolerance=1e-6)
            if polygon_wgs84 is None:
                continue
            projected_coords = [_project(coord) for coord in building_coords]
            polygon_projected = _build_polygon(projected_coords, tolerance=0.05)
            if polygon_projected is None:
                continue
            height = _resolve_height(tags)
            buildings.append(
                BuildingFootprint(
                    osm_id=element["id"],
                    polygon_projected=polygon_projected,
                    polygon_wgs84=polygon_wgs84,
                    height_m=height,
                    name=tags.get("name"),
                    tags=tags,
                )
            )
        elif _is_park(tags):
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            projected_coords = [_project(coord) for coord in coords]
            polygon_projected = _build_polygon(projected_coords, tolerance=0.25)
            if polygon_projected is None:
                continue
            parks.append(
                ParkFeature(
                    osm_id=element["id"],
                    polygon_projected=polygon_projected,
                    tags=tags,
                )
            )
        elif _is_water(tags):
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            projected_coords = [_project(coord) for coord in coords]
            polygon_projected = _build_polygon(projected_coords, tolerance=0.25)
            if polygon_projected is None:
                continue
            waters.append(
                WaterFeature(
                    osm_id=element["id"],
                    polygon_projected=polygon_projected,
                    tags=tags,
                )
            )
        elif "highway" in tags:
            projected_coords = [_project(coord) for coord in coords]
            line_projected = _build_linestring(projected_coords, tolerance=0.25)
            if line_projected is None:
                continue
            roads.append(
                RoadFeature(
                    osm_id=element["id"],
                    line_projected=line_projected,
                    tags=tags,
                )
            )
    return buildings, roads, parks, waters


def _build_polygon(
    coords: list[tuple[float, float]], *, tolerance: float | None = None
) -> Polygon | None:
    polygon = Polygon(coords)
    if not polygon.is_valid:
        polygon = make_valid(polygon)
    if polygon.is_empty or not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty:
        return None
    if tolerance:
        polygon = polygon.simplify(tolerance, preserve_topology=True)
    return polygon


def _build_linestring(
    coords: list[tuple[float, float]], *, tolerance: float | None = None
) -> LineString | None:
    line = LineString(coords)
    if line.is_empty or not line.is_valid or line.length == 0:
        return None
    if tolerance:
        line = line.simplify(tolerance, preserve_topology=True)
    return line


def _project(coord: tuple[float, float]) -> tuple[float, float]:
    lon, lat = coord
    x, y = _transformer.transform(lon, lat)
    return (float(x), float(y))


def project_point(lon: float, lat: float) -> tuple[float, float]:
    return _project((lon, lat))


def _inverse_project(x: float, y: float) -> tuple[float, float]:
    lon, lat = _inverse_transformer.transform(x, y)
    return (float(lon), float(lat))


def _lonlat_from_node(
    node_index: dict[int, tuple[float, float]], node_id: int
) -> tuple[float, float] | None:
    return node_index.get(node_id)


def _resolve_height(tags: dict[str, Any]) -> float:
    settings = get_settings()
    height = _parse_float(tags.get("height"))
    if height:
        return max(settings.min_height_m, float(height))

    levels = _parse_float(tags.get("building:levels"))
    if levels:
        return max(settings.min_height_m, float(levels) * settings.level_height_m)

    return settings.default_height_m


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"([0-9]+(?:\\.[0-9]+)?)", str(value))
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def _is_park(tags: dict[str, Any]) -> bool:
    leisure = tags.get("leisure")
    landuse = tags.get("landuse")
    return leisure == "park" or landuse in {"grass", "recreation_ground", "meadow"}


def _is_water(tags: dict[str, Any]) -> bool:
    natural = tags.get("natural")
    water = tags.get("water")
    waterway = tags.get("waterway")
    landuse = tags.get("landuse")
    return (
        natural == "water"
        or water in {"lake", "pond", "reservoir"}
        or waterway == "riverbank"
        or landuse == "reservoir"
    )


def _build_tiles(
    longitude: float, latitude: float, radius_m: int, tile_size_m: int
) -> list[tuple[float, float, float, float]]:
    center_x, center_y = _project((longitude, latitude))
    min_x = center_x - radius_m
    max_x = center_x + radius_m
    min_y = center_y - radius_m
    max_y = center_y + radius_m

    tiles: list[tuple[float, float, float, float]] = []
    y = min_y
    while y < max_y:
        next_y = min(y + tile_size_m, max_y)
        x = min_x
        while x < max_x:
            next_x = min(x + tile_size_m, max_x)
            west, south = _inverse_project(x, y)
            east, north = _inverse_project(next_x, next_y)
            tiles.append(
                (
                    min(south, north),
                    min(west, east),
                    max(south, north),
                    max(west, east),
                )
            )
            x = next_x
        y = next_y
    return tiles
