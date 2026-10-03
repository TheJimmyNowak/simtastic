from __future__ import annotations

import math
import struct
from functools import lru_cache
from pathlib import Path

from geopy.distance import geodesic

_SAMPLES = 1201
_TILE_CACHE: dict[tuple[str, str], list[int]] = {}


def _tile_name(lat: float, lon: float) -> str:
    lat_i = math.floor(lat)
    lon_i = math.floor(lon)
    ns = f"N{lat_i:02d}" if lat >= 0 else f"S{abs(lat_i):02d}"
    ew = f"E{lon_i:03d}" if lon >= 0 else f"W{abs(lon_i):03d}"
    return f"{ns}{ew}"


@lru_cache(maxsize=128)
def _read_tile(cache_dir: str, tile: str) -> tuple[int, ...]:
    path = Path(cache_dir) / f"{tile}.hgt"
    if not path.exists():
        raise FileNotFoundError(f"SRTM tile missing: {path}")
    raw = path.read_bytes()
    if len(raw) < _SAMPLES * _SAMPLES * 2:
        raise ValueError(f"Invalid HGT size for {path}")
    return struct.unpack(f">{_SAMPLES * _SAMPLES}h", raw[: _SAMPLES * _SAMPLES * 2])


def _elevation_m(cache_dir: Path, lat: float, lon: float) -> float:
    tile = _tile_name(lat, lon)
    data = _read_tile(str(cache_dir.resolve()), tile)
    lat_sw = math.floor(lat)
    lon_sw = math.floor(lon)
    if lat >= 0:
        row = int(round((lat_sw + 1.0 - lat) * (_SAMPLES - 1)))
    else:
        row = int(round((lat - lat_sw) * (_SAMPLES - 1)))
    col = int(round((lon - lon_sw) * (_SAMPLES - 1)))
    row = max(0, min(_SAMPLES - 1, row))
    col = max(0, min(_SAMPLES - 1, col))
    val = data[row * _SAMPLES + col]
    if val == -32768:
        return 0.0
    return float(val)


def build_terrain_profile(
    cache_dir: Path,
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    num_segments: int = 50,
) -> tuple[list[float], float]:
    """Sample terrain AMSL (m) along a geodesic; return elevations and distance_m."""
    if num_segments < 1:
        num_segments = 1
    dist_m = float(geodesic((lat1, lon1), (lat2, lon2)).meters)
    profile: list[float] = []
    for i in range(num_segments + 1):
        f = i / num_segments
        lat = lat1 + f * (lat2 - lat1)
        lon = lon1 + f * (lon2 - lon1)
        profile.append(_elevation_m(cache_dir, lat, lon))
    return profile, dist_m
