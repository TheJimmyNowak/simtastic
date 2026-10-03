from pathlib import Path

import pytest

from app.services.propagation.building_index import BuildingIndex, wgs84_to_epsg2180
from app.services.propagation.building_model import (
    NodeMount,
    compute_building_path,
    first_fresnel_radius_m,
    knife_edge_loss_db,
    resolve_node_mount,
)
from app.models.environment import EnvironmentConfig
from app.models.node import NodeConfig
from app.services.propagation.link_matrix import compute_scenario_fingerprint


def _citygml(buildings: list[tuple[str, list[tuple[float, float]], float, float]]) -> str:
    members = []
    for building_id, footprint, ground, roof in buildings:
        closed = footprint + [footprint[0]]
        ground_pos = " ".join(f"{x} {y} {ground}" for x, y in closed)
        roof_pos = " ".join(f"{x} {y} {roof}" for x, y in closed)
        members.append(
            f"""
            <cityObjectMember><bldg:Building gml:id="{building_id}">
              <gen:stringAttribute name="aktZrodla"><gen:value>2024</gen:value></gen:stringAttribute>
              <bldg:measuredHeight uom="meter">{roof-ground}</bldg:measuredHeight>
              <bldg:lod1Solid><gml:Solid><gml:exterior><gml:CompositeSurface>
                <gml:surfaceMember><gml:Polygon><gml:exterior><gml:LinearRing><gml:posList>{ground_pos}</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
                <gml:surfaceMember><gml:Polygon><gml:exterior><gml:LinearRing><gml:posList>{roof_pos}</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
              </gml:CompositeSurface></gml:exterior></gml:Solid></bldg:lod1Solid>
            </bldg:Building></cityObjectMember>
            """
        )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <CityModel xmlns="http://www.opengis.net/citygml/2.0"
      xmlns:gml="http://www.opengis.net/gml"
      xmlns:bldg="http://www.opengis.net/citygml/building/2.0"
      xmlns:gen="http://www.opengis.net/citygml/generics/2.0">
      {''.join(members)}
    </CityModel>"""


def _index(tmp_path: Path, buildings) -> BuildingIndex:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "test.gml").write_text(_citygml(buildings), encoding="utf-8")
    index = BuildingIndex(tmp_path / "buildings.sqlite")
    index.build(raw)
    return index


def test_epsg2180_conversion_matches_reference() -> None:
    x, y = wgs84_to_epsg2180(52.2297, 21.0122)
    assert x == pytest.approx(637382.20, abs=0.1)
    assert y == pytest.approx(486757.21, abs=0.1)


def test_node_inside_footprint_is_mounted_above_roof(tmp_path: Path) -> None:
    x, y = wgs84_to_epsg2180(52.2297, 21.0122)
    index = _index(tmp_path, [("roof-node", [(x-5, y-5), (x+5, y-5), (x+5, y+5), (x-5, y+5)], 100, 112)])
    mount = resolve_node_mount(index, 52.2297, 21.0122, terrain_z_m=99, mast_height_m=3)
    assert mount.placement == "rooftop"
    assert mount.surface_z_m == pytest.approx(111)
    assert mount.antenna_z_m == pytest.approx(114)
    assert mount.building_id == "roof-node"


def test_dominant_building_blocks_fresnel_zone(tmp_path: Path) -> None:
    index = _index(tmp_path, [("obstacle", [(45, -5), (55, -5), (55, 5), (45, 5)], 0, 30)])
    tx = NodeMount(0, 0, 20, 0, "terrain", None)
    rx = NodeMount(100, 0, 20, 0, "terrain", None)
    result = compute_building_path(index, tx, rx, frequency_mhz=868.0)
    assert result.blocked_count == 1
    assert result.dominant_building_id == "obstacle"
    assert result.loss_db > 10
    assert result.min_fresnel_clearance_m < 0


def test_endpoint_building_is_not_its_own_obstacle(tmp_path: Path) -> None:
    index = _index(tmp_path, [("tx-roof", [(-5, -5), (5, -5), (5, 5), (-5, 5)], 0, 15)])
    tx = NodeMount(0, 0, 17, 15, "rooftop", "tx-roof")
    rx = NodeMount(100, 0, 17, 0, "terrain", None)
    result = compute_building_path(index, tx, rx, frequency_mhz=868.0)
    assert result.blocked_count == 0
    assert result.loss_db == 0


def test_fresnel_and_knife_edge_helpers() -> None:
    assert first_fresnel_radius_m(868, 50, 50) > 2
    assert knife_edge_loss_db(10, 868, 50, 50) > 10
    assert knife_edge_loss_db(-20, 868, 50, 50) == 0


def test_scenario_fingerprint_ignores_runtime_fields_but_tracks_building_model() -> None:
    node = NodeConfig(id="a", lat=52, lon=21)
    environment = EnvironmentConfig(building_model_enabled=True)
    original = compute_scenario_fingerprint([node], environment)
    node.hop_limit = 5
    assert compute_scenario_fingerprint([node], environment.model_copy(update={"fading_sigma_db": 3.0})) == original
    changed = environment.model_copy(update={"fresnel_clearance_ratio": 0.8})
    assert compute_scenario_fingerprint([node], changed) != original
