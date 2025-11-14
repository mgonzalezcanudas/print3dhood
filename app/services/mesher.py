from __future__ import annotations

import json
from dataclasses import dataclass
from io import BytesIO
from typing import Iterable
import zipfile

import shapely.affinity as affinity
from shapely.geometry import GeometryCollection, MultiPolygon, Point, Polygon
from shapely.ops import unary_union
import trimesh

from ..config import get_settings
from ..models import (
    BuildingSummary,
    LayerInfo,
    LayerPreview,
    ModelMetadata,
    ModelRequest,
    PolygonPath,
)
from .overpass import (
    BuildingFootprint,
    ParkFeature,
    RoadFeature,
    WaterFeature,
    project_point,
)


class ModelBuildError(RuntimeError):
    """Raised when a mesh cannot be produced."""


@dataclass
class PreparedGeometries:
    base_circle: Polygon
    water_union: Polygon | MultiPolygon | GeometryCollection
    land_no_water: Polygon | MultiPolygon | GeometryCollection
    park_union: Polygon | MultiPolygon | GeometryCollection
    building_base: Polygon | MultiPolygon | GeometryCollection
    road_union: Polygon | MultiPolygon | GeometryCollection
    building_mesh_data: list[tuple[list[Polygon], float]]
    building_summaries: list[BuildingSummary]
    home_polygons: list[Polygon]
    home_height: float | None


@dataclass
class SceneContext:
    settings: Settings
    origin_x: float
    origin_y: float
    scale_factor: float
    print_radius: float
    prepared: PreparedGeometries


def build_model_archive(
    *,
    request: ModelRequest,
    buildings: list[BuildingFootprint],
    roads: list[RoadFeature],
    parks: list[ParkFeature],
    waters: list[WaterFeature],
) -> tuple[str, BytesIO, ModelMetadata]:
    if not buildings:
        raise ModelBuildError("No buildings were found for the requested area.")

    context = _build_scene_context(
        request=request,
        buildings=buildings,
        roads=roads,
        parks=parks,
        waters=waters,
    )
    settings = context.settings
    prepared = context.prepared
    print_radius = context.print_radius
    scale_factor = context.scale_factor

    layer_meshes: dict[str, trimesh.Trimesh] = {}

    water_mesh = _build_water_layer(
        base_circle=prepared.base_circle,
        water_union=prepared.water_union,
        settings=settings,
    )
    layer_meshes["water_layer"] = water_mesh

    green_mesh = _build_green_layer(
        land_geom=prepared.land_no_water,
        park_union=prepared.park_union,
        settings=settings,
    )
    layer_meshes["green_layer"] = green_mesh

    building_mesh = _build_building_layer(
        building_base=prepared.building_base,
        road_union=prepared.road_union,
        building_mesh_data=prepared.building_mesh_data,
        home_polygons=prepared.home_polygons
        if request.highlight_home and prepared.home_polygons
        else [],
        settings=settings,
    )
    layer_meshes["building_layer"] = building_mesh

    highlight_mesh = None
    if (
        request.highlight_home
        and settings.highlight_enabled
        and prepared.home_polygons
        and prepared.home_height
    ):
        highlight_mesh = _build_highlight_layer(
            polygons=prepared.home_polygons,
            building_height=prepared.home_height,
            settings=settings,
        )
        layer_meshes["highlight_layer"] = highlight_mesh

    layer_infos = _build_layer_info(settings, bool(highlight_mesh))

    metadata = ModelMetadata(
        building_count=len(prepared.building_summaries),
        radius_meters=int(request.radius_meters),
        highlighted=bool(highlight_mesh),
        formats=request.formats,
        origin=(context.origin_x, context.origin_y),
        scale_ratio=scale_factor,
        layers=layer_infos,
        buildings=prepared.building_summaries,
    )

    mesh_buffer = BytesIO()
    with zipfile.ZipFile(mesh_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for fmt in request.formats:
            for layer_name, mesh in layer_meshes.items():
                archive.writestr(
                    f"layers/{layer_name}.{fmt}",
                    _export_mesh(mesh, fmt),
                )
        archive.writestr(
            "metadata.json",
            json.dumps(metadata.model_dump(), indent=2),
        )

    mesh_buffer.seek(0)
    filename = f"print3dhood_{int(request.radius_meters)}m_layers.zip"
    return filename, mesh_buffer, metadata


def generate_preview_data(
    *,
    request: ModelRequest,
    buildings: list[BuildingFootprint],
    roads: list[RoadFeature],
    parks: list[ParkFeature],
    waters: list[WaterFeature],
) -> tuple[ModelMetadata, list[LayerPreview]]:
    if not buildings:
        raise ModelBuildError("No buildings were found for the requested area.")

    context = _build_scene_context(
        request=request,
        buildings=buildings,
        roads=roads,
        parks=parks,
        waters=waters,
    )
    prepared = context.prepared
    settings = context.settings

    layer_infos = _build_layer_info(settings, bool(prepared.home_polygons))
    metadata = ModelMetadata(
        building_count=len(prepared.building_summaries),
        radius_meters=int(request.radius_meters),
        highlighted=bool(prepared.home_polygons),
        formats=["stl"],
        origin=(context.origin_x, context.origin_y),
        scale_ratio=context.scale_factor,
        layers=layer_infos,
        buildings=prepared.building_summaries,
    )
    previews = _create_layer_previews(prepared, context.print_radius, settings)
    return metadata, previews


def _build_scene_context(
    *,
    request: ModelRequest,
    buildings: list[BuildingFootprint],
    roads: list[RoadFeature],
    parks: list[ParkFeature],
    waters: list[WaterFeature],
) -> SceneContext:
    settings = get_settings()
    origin_x, origin_y = project_point(request.longitude, request.latitude)
    base_radius_world = float(request.radius_meters + settings.base_padding_m)
    print_radius = max(settings.target_print_diameter_m / 2, 0.001)
    scale_factor = (
        print_radius / base_radius_world if base_radius_world > 0 else 1.0
    )

    circle_world = Point(origin_x, origin_y).buffer(
        float(request.radius_meters), resolution=96
    )
    base_circle = Point(0, 0).buffer(print_radius, resolution=128)
    home_point = Point(request.longitude, request.latitude)
    home_building = _identify_home_building(buildings, home_point)

    prepared = _prepare_geometries(
        buildings=buildings,
        roads=roads,
        parks=parks,
        waters=waters,
        circle_world=circle_world,
        base_circle=base_circle,
        origin_x=origin_x,
        origin_y=origin_y,
        scale_factor=scale_factor,
        home_building=home_building,
        settings=settings,
    )

    return SceneContext(
        settings=settings,
        origin_x=origin_x,
        origin_y=origin_y,
        scale_factor=scale_factor,
        print_radius=print_radius,
        prepared=prepared,
    )


def _prepare_geometries(
    *,
    buildings: list[BuildingFootprint],
    roads: list[RoadFeature],
    parks: list[ParkFeature],
    waters: list[WaterFeature],
    circle_world: Polygon,
    base_circle: Polygon,
    origin_x: float,
    origin_y: float,
    scale_factor: float,
    home_building: BuildingFootprint | None,
    settings,
) -> PreparedGeometries:
    building_mesh_data: list[tuple[list[Polygon], float]] = []
    building_polygons_all: list[Polygon] = []
    building_summaries: list[BuildingSummary] = []
    home_polygons: list[Polygon] = []
    home_height: float | None = None

    for footprint in buildings:
        clipped = footprint.polygon_projected.intersection(circle_world)
        if clipped.is_empty:
            continue
        local = _to_local_scaled(clipped, origin_x, origin_y, scale_factor)
        polygons = [poly for poly in _iter_polygons(local) if not poly.is_empty]
        if not polygons:
            continue
        scaled_height = max(footprint.height_m * scale_factor, 0.0005)
        building_summaries.append(
            BuildingSummary(
                osm_id=footprint.osm_id,
                height_m=footprint.height_m,
                footprint_area_m2=footprint.area_m2,
                name=footprint.name,
            )
        )
        if home_building and footprint.osm_id == home_building.osm_id:
            home_polygons = polygons
            home_height = scaled_height
        else:
            building_mesh_data.append((polygons, scaled_height))
        building_polygons_all.extend(polygons)

    if not building_polygons_all and not home_polygons:
        raise ModelBuildError("Unable to construct scaled footprints for this area.")

    park_polygons: list[Polygon] = []
    park_shrink = max(settings.park_indent_shrink_m * scale_factor, 0.0)
    for feature in parks:
        local = _to_local_scaled(feature.polygon_projected, origin_x, origin_y, scale_factor)
        for polygon in _iter_polygons(local):
            if park_shrink > 0:
                shrunken = polygon.buffer(-park_shrink)
                for shp in _iter_polygons(shrunken):
                    park_polygons.append(shp)
            else:
                park_polygons.append(polygon)
    water_polygons = [
        poly
        for feature in waters
        for poly in _iter_polygons(
            _to_local_scaled(feature.polygon_projected, origin_x, origin_y, scale_factor)
        )
    ]
    road_buffers = []
    road_width = max(settings.road_indent_width_m * scale_factor, 0.001)
    for road in roads:
        local_line = _to_local_scaled(road.line_projected, origin_x, origin_y, scale_factor)
        buffered = local_line.buffer(road_width, cap_style=2, join_style=2)
        if not buffered.is_empty:
            road_buffers.extend(_iter_polygons(buffered))

    building_union = _clip_to_circle(
        _union_geometries(building_polygons_all + home_polygons), base_circle
    )
    water_union = _clip_to_circle(_union_geometries(water_polygons), base_circle)
    land_no_water = base_circle
    if not getattr(water_union, "is_empty", True):
        land_no_water = base_circle.difference(water_union)
    if getattr(land_no_water, "is_empty", True):
        land_no_water = base_circle

    parks_union = _clip_to_circle(_union_geometries(park_polygons), land_no_water)
    road_union = _clip_to_circle(_union_geometries(road_buffers), base_circle)

    building_base = land_no_water
    if not getattr(parks_union, "is_empty", True):
        building_base = building_base.difference(parks_union)
    if getattr(building_base, "is_empty", True):
        building_base = land_no_water

    return PreparedGeometries(
        base_circle=base_circle,
        water_union=water_union,
        land_no_water=land_no_water,
        park_union=parks_union,
        building_base=building_base,
        road_union=road_union,
        building_mesh_data=building_mesh_data,
        building_summaries=building_summaries,
        home_polygons=home_polygons,
        home_height=home_height,
    )


def _build_water_layer(
    *,
    base_circle: Polygon,
    water_union: Polygon | MultiPolygon | GeometryCollection,
    settings,
) -> trimesh.Trimesh:
    base_height = settings.base_thickness_m
    meshes: list[trimesh.Trimesh] = [
        _extrude_polygon(base_circle, base_height),
    ]

    if not getattr(water_union, "is_empty", True):
        water_height = settings.base_thickness_m * 2
        for polygon in _iter_polygons(water_union):
            column = _extrude_polygon(polygon, water_height)
            column.apply_translation((0, 0, base_height))
            meshes.append(column)

    return _combine_meshes(meshes)


def _build_green_layer(
    *,
    land_geom: Polygon | MultiPolygon | GeometryCollection,
    park_union: Polygon | MultiPolygon | GeometryCollection,
    settings,
) -> trimesh.Trimesh:
    base_height = settings.green_layer_thickness_m
    if getattr(land_geom, "is_empty", True):
        raise ModelBuildError("No geometry available for the green layer.")

    meshes: list[trimesh.Trimesh] = []
    for polygon in _iter_polygons(land_geom):
        meshes.append(_extrude_polygon(polygon, base_height))

    if not getattr(park_union, "is_empty", True):
        for polygon in _iter_polygons(park_union):
            extrusion = _extrude_polygon(polygon, settings.green_layer_thickness_m)
            extrusion.apply_translation((0, 0, base_height))
            meshes.append(extrusion)

    return _combine_meshes(meshes)


def _build_layer_info(settings: Settings, include_highlight: bool) -> list[LayerInfo]:
    infos = [
        LayerInfo(
            name="water_layer",
            thickness_m=settings.base_thickness_m,
            description="Water/base disk (thickness x) with water bodies extruded 2x to reach street level.",
        ),
        LayerInfo(
            name="green_layer",
            thickness_m=settings.green_layer_thickness_m,
            description="Land disk (thickness x) with holes for water extrusions plus parks raised another x.",
        ),
        LayerInfo(
            name="building_layer",
            thickness_m=settings.building_layer_thickness_m,
            description="Street disk (thickness x) with cavities for water/green extrusions and buildings rising above.",
        ),
    ]
    if include_highlight:
        infos.append(
            LayerInfo(
                name="highlight_layer",
                thickness_m=settings.building_layer_thickness_m,
                description="Removable home building with a base that keys into the cavity on the buildings layer.",
            )
        )
    return infos


def _create_layer_previews(
    prepared: PreparedGeometries, print_radius: float, settings: Settings
) -> list[LayerPreview]:
    previews: list[LayerPreview] = []
    previews.append(
        LayerPreview(
            name="water_layer",
            thickness_m=settings.base_thickness_m,
            base_color="#bfdbfe",
            feature_color="#1d4ed8",
            description="Water/base disk (thickness x) with water bodies extruded 2x to reach street level.",
            base_paths=_geometry_to_paths(prepared.base_circle, print_radius),
            feature_paths=_geometry_to_paths(prepared.water_union, print_radius),
        )
    )
    previews.append(
        LayerPreview(
            name="green_layer",
            thickness_m=settings.green_layer_thickness_m,
            base_color="#dcfce7",
            feature_color="#16a34a",
            description="Land disk (thickness x) with holes for water extrusions plus parks raised another x.",
            base_paths=_geometry_to_paths(prepared.land_no_water, print_radius),
            feature_paths=_geometry_to_paths(prepared.park_union, print_radius),
        )
    )

    building_polys = [
        polygon
        for polygons, _ in prepared.building_mesh_data
        for polygon in polygons
    ]
    previews.append(
        LayerPreview(
            name="building_layer",
            thickness_m=settings.building_layer_thickness_m,
            base_color="#e5e7eb",
            feature_color="#111827",
            overlay_color="#d1d5db",
            description="Street disk (thickness x) with cavities for water/green extrusions and buildings rising above.",
            base_paths=_geometry_to_paths(prepared.building_base, print_radius),
            feature_paths=_geometry_to_paths(_union_geometries(building_polys), print_radius),
            overlay_paths=_geometry_to_paths(prepared.road_union, print_radius),
        )
    )

    if prepared.home_polygons:
        previews.append(
            LayerPreview(
                name="highlight_layer",
                thickness_m=settings.building_layer_thickness_m,
                base_color="#fed7aa",
                feature_color="#f97316",
                description="Removable home building with a base that keys into the cavity on the buildings layer.",
                base_paths=_geometry_to_paths(
                    _union_geometries(prepared.home_polygons), print_radius
                ),
            )
        )

    return previews


def _geometry_to_paths(
    geometry: Polygon | MultiPolygon | GeometryCollection | None, radius: float
) -> list[PolygonPath]:
    if geometry is None or getattr(geometry, "is_empty", True):
        return []
    paths: list[PolygonPath] = []
    for polygon in _iter_polygons(geometry):
        outer = [_normalize_point(x, y, radius) for x, y in polygon.exterior.coords]
        holes = [
            [_normalize_point(x, y, radius) for x, y in interior.coords]
            for interior in polygon.interiors
        ]
        paths.append(PolygonPath(outer=outer, holes=holes))
    return paths


def _normalize_point(x: float, y: float, radius: float) -> tuple[float, float]:
    if radius == 0:
        return (0.0, 0.0)
    nx = (x + radius) / (2 * radius)
    ny = 1 - ((y + radius) / (2 * radius))
    return (nx, ny)


def _build_building_layer(
    *,
    building_base: Polygon | MultiPolygon | GeometryCollection,
    road_union: Polygon | MultiPolygon | GeometryCollection,
    building_mesh_data: list[tuple[list[Polygon], float]],
    home_polygons: list[Polygon],
    settings,
) -> trimesh.Trimesh:
    if getattr(building_base, "is_empty", True):
        raise ModelBuildError("No geometry available for the building layer.")

    base_geom = building_base
    if home_polygons:
        base_geom = base_geom.difference(_union_geometries(home_polygons))

    road_indent = min(
        settings.road_groove_depth_m,
        settings.building_layer_thickness_m * 0.8,
    )
    base_height = settings.building_layer_thickness_m
    slab_height = max(base_height - road_indent, 0.0005)

    meshes: list[trimesh.Trimesh] = []
    for polygon in _iter_polygons(base_geom):
        meshes.append(_extrude_polygon(polygon, slab_height))

    if road_indent > 0:
        top_geom = base_geom
        if not getattr(road_union, "is_empty", True):
            top_geom = top_geom.difference(road_union)
        for polygon in _iter_polygons(top_geom):
            road_mesh = _extrude_polygon(polygon, road_indent)
            road_mesh.apply_translation((0, 0, slab_height))
            meshes.append(road_mesh)

    for polygons, height in building_mesh_data:
        for polygon in polygons:
            if polygon.is_empty or polygon.area == 0:
                continue
            mesh = _extrude_polygon(polygon, height)
            mesh.apply_translation((0, 0, base_height))
            meshes.append(mesh)

    return _combine_meshes(meshes)


def _build_highlight_layer(
    *, polygons: list[Polygon], building_height: float, settings
) -> trimesh.Trimesh:
    if not polygons:
        raise ModelBuildError("Highlight geometry is missing.")
    peg_depth = min(settings.highlight_peg_depth_m, settings.building_layer_thickness_m)

    meshes: list[trimesh.Trimesh] = []
    for polygon in polygons:
        if polygon.is_empty:
            continue
        if peg_depth > 0:
            peg = _extrude_polygon(polygon, peg_depth)
            meshes.append(peg)
        body = _extrude_polygon(polygon, building_height)
        body.apply_translation((0, 0, peg_depth))
        meshes.append(body)

    return _combine_meshes(meshes)


def _to_local_scaled(
    geometry, origin_x: float, origin_y: float, scale_factor: float
):
    local = affinity.translate(geometry, xoff=-origin_x, yoff=-origin_y)
    return affinity.scale(local, xfact=scale_factor, yfact=scale_factor, origin=(0, 0))


def _union_geometries(geometries: list[Polygon]) -> Polygon | MultiPolygon | GeometryCollection:
    valid = [geom for geom in geometries if geom and not geom.is_empty]
    if not valid:
        return GeometryCollection()
    return unary_union(valid)


def _clip_to_circle(
    geometry: Polygon | MultiPolygon | GeometryCollection | None, circle: Polygon
) -> Polygon | MultiPolygon | GeometryCollection:
    if geometry is None or getattr(geometry, "is_empty", True):
        return GeometryCollection()
    return geometry.intersection(circle)


def _combine_meshes(meshes: list[trimesh.Trimesh]) -> trimesh.Trimesh:
    filtered = [mesh for mesh in meshes if mesh is not None and len(mesh.vertices)]
    if not filtered:
        raise ModelBuildError("Layer mesh could not be constructed.")
    if len(filtered) == 1:
        return filtered[0]
    return trimesh.util.concatenate(filtered)


def _export_mesh(mesh: trimesh.Trimesh, filetype: str) -> bytes:
    payload = mesh.export(file_type=filetype)
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return payload


def _identify_home_building(
    buildings: Iterable[BuildingFootprint], point: Point
) -> BuildingFootprint | None:
    containing = [
        footprint for footprint in buildings if footprint.polygon_wgs84.contains(point)
    ]
    if containing:
        return containing[0]

    def _distance(footprint: BuildingFootprint) -> float:
        return footprint.polygon_wgs84.centroid.distance(point)

    try:
        return min(buildings, key=_distance)
    except ValueError:
        return None


def _iter_polygons(geometry) -> list[Polygon]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, (MultiPolygon, GeometryCollection)):
        return [
            geom
            for geom in geometry.geoms
            if isinstance(geom, Polygon) and not geom.is_empty
        ]
    return []


def _extrude_polygon(polygon: Polygon, height: float) -> trimesh.Trimesh:
    return trimesh.creation.extrude_polygon(polygon, height, triangulation="earcut")
