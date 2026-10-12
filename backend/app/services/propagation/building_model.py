from __future__ import annotations

import math
from dataclasses import dataclass

from app.services.propagation.building_index import Building, BuildingIndex, wgs84_to_epsg2180

SPEED_OF_LIGHT_M_S = 299_792_458.0


@dataclass(frozen=True)
class NodeMount:
    x: float
    y: float
    antenna_z_m: float
    surface_z_m: float
    placement: str
    building_id: str | None


@dataclass(frozen=True)
class BuildingPathResult:
    loss_db: float = 0.0
    blocked_count: int = 0
    min_fresnel_clearance_m: float = math.inf
    dominant_building_id: str | None = None
    data_year: str | None = None


def point_in_polygon(x: float, y: float, polygon: tuple[tuple[float, float], ...]) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:]):
        if _point_segment_distance(x, y, x1, y1, x2, y2) < 0.05:
            return True
        if (y1 > y) != (y2 > y):
            intersection_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x <= intersection_x:
                inside = not inside
    return inside


def resolve_node_mount(
    index: BuildingIndex,
    lat: float,
    lon: float,
    terrain_z_m: float,
    mast_height_m: float,
) -> NodeMount:
    x, y = wgs84_to_epsg2180(lat, lon)
    candidates = index.query(x - 0.2, x + 0.2, y - 0.2, y + 0.2)
    containing = [building for building in candidates if point_in_polygon(x, y, building.footprint)]
    if containing:
        building = max(containing, key=lambda item: item.roof_z_m)
        # Put the roof on the same NMT ground that the ITM profile uses. NMT heights are
        # PL-KRON86-NH and CityGML PL-EVRF2007-NH (centimetres apart), and the relative
        # measuredHeight keeps one source of ground height for the whole link.
        roof_z = terrain_z_m + building.height_m
        return NodeMount(x, y, roof_z + mast_height_m, roof_z, "rooftop", building.source_id)
    return NodeMount(x, y, terrain_z_m + mast_height_m, terrain_z_m, "terrain", None)


def first_fresnel_radius_m(frequency_mhz: float, d1_m: float, d2_m: float) -> float:
    if d1_m <= 0 or d2_m <= 0:
        return 0.0
    wavelength = SPEED_OF_LIGHT_M_S / (frequency_mhz * 1_000_000.0)
    return math.sqrt(wavelength * d1_m * d2_m / (d1_m + d2_m))


def knife_edge_loss_db(
    obstruction_height_m: float, frequency_mhz: float, d1_m: float, d2_m: float
) -> float:
    if d1_m <= 0 or d2_m <= 0:
        return 0.0
    wavelength = SPEED_OF_LIGHT_M_S / (frequency_mhz * 1_000_000.0)
    v = obstruction_height_m * math.sqrt(2.0 * (d1_m + d2_m) / (wavelength * d1_m * d2_m))
    if v <= -0.78:
        return 0.0
    return 6.9 + 20.0 * math.log10(math.sqrt((v - 0.1) ** 2 + 1.0) + v - 0.1)


def _point_segment_distance(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _segment_intersection_t(a, b, c, d) -> float | None:
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    denominator = rx * sy - ry * sx
    if abs(denominator) < 1e-12:
        return None
    qx, qy = c[0] - a[0], c[1] - a[1]
    t = (qx * sy - qy * sx) / denominator
    u = (qx * ry - qy * rx) / denominator
    return t if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0 else None


def _closest_approach_t(start, end, polygon) -> tuple[float, float]:
    intersections = [
        value
        for p1, p2 in zip(polygon, polygon[1:])
        if (value := _segment_intersection_t(start, end, p1, p2)) is not None
    ]
    if intersections:
        return sum(intersections) / len(intersections), 0.0
    dx, dy = end[0] - start[0], end[1] - start[1]
    length2 = dx * dx + dy * dy
    best_t, best_distance = 0.0, math.inf
    for x, y in polygon:
        t = max(0.0, min(1.0, ((x - start[0]) * dx + (y - start[1]) * dy) / length2))
        distance = math.hypot(x - (start[0] + t * dx), y - (start[1] + t * dy))
        if distance < best_distance:
            best_t, best_distance = t, distance
    return best_t, best_distance


def compute_building_path(
    index: BuildingIndex,
    tx: NodeMount,
    rx: NodeMount,
    frequency_mhz: float,
    clearance_ratio: float = 0.6,
    terrain_profile_m: list[float] | None = None,
) -> BuildingPathResult:
    distance = math.hypot(rx.x - tx.x, rx.y - tx.y)
    if distance <= 0.1:
        return BuildingPathResult()
    wavelength = SPEED_OF_LIGHT_M_S / (frequency_mhz * 1_000_000.0)
    max_radius = math.sqrt(wavelength * distance / 4.0)
    candidates = index.query(
        min(tx.x, rx.x) - max_radius,
        max(tx.x, rx.x) + max_radius,
        min(tx.y, rx.y) - max_radius,
        max(tx.y, rx.y) + max_radius,
    )
    blocked: list[tuple[float, float, Building]] = []
    minimum_clearance = math.inf
    endpoint_ids = {tx.building_id, rx.building_id} - {None}
    for building in candidates:
        if building.source_id in endpoint_ids:
            continue
        t, horizontal_offset = _closest_approach_t((tx.x, tx.y), (rx.x, rx.y), building.footprint)
        if t <= 1e-6 or t >= 1.0 - 1e-6:
            continue
        d1, d2 = distance * t, distance * (1.0 - t)
        radius = first_fresnel_radius_m(frequency_mhz, d1, d2)
        if horizontal_offset > radius:
            continue
        vertical_radius = math.sqrt(max(0.0, radius * radius - horizontal_offset * horizontal_offset))
        los_z = tx.antenna_z_m + t * (rx.antenna_z_m - tx.antenna_z_m)
        if terrain_profile_m:
            position = t * (len(terrain_profile_m) - 1)
            left = min(int(position), len(terrain_profile_m) - 1)
            right = min(left + 1, len(terrain_profile_m) - 1)
            fraction = position - left
            terrain_z = terrain_profile_m[left] + fraction * (
                terrain_profile_m[right] - terrain_profile_m[left]
            )
            roof_z = terrain_z + building.height_m
        else:
            roof_z = building.roof_z_m
        clearance = los_z - roof_z
        fresnel_clearance = clearance - clearance_ratio * vertical_radius
        minimum_clearance = min(minimum_clearance, fresnel_clearance)
        if fresnel_clearance < 0:
            loss = knife_edge_loss_db(roof_z - los_z, frequency_mhz, d1, d2)
            blocked.append((loss, fresnel_clearance, building))
    if not blocked:
        return BuildingPathResult(min_fresnel_clearance_m=minimum_clearance)
    dominant = max(blocked, key=lambda item: item[0])
    return BuildingPathResult(
        loss_db=dominant[0],
        blocked_count=len(blocked),
        min_fresnel_clearance_m=minimum_clearance,
        dominant_building_id=dominant[2].source_id,
        data_year=dominant[2].data_year,
    )
