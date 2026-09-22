"""地理计算工具：局部等距圆柱投影 + 点到线段投影。

不依赖 shapely/pyproj，保证镜像精简、构建零编译。
在路网尺度（< 几十公里）上误差可忽略。
"""
from __future__ import annotations

import math
from typing import Sequence, Tuple

EARTH_RADIUS_M = 6_371_000.0
_M_PER_DEG_LAT = 111_320.0

Point = Tuple[float, float]  # (lon, lat)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两经纬度点间大圆距离（米）。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


class LocalProjector:
    """以参考纬度为中心的等距圆柱投影，把 lon/lat 换成局部平面米坐标。"""

    def __init__(self, ref_lat: float):
        self._kx = _M_PER_DEG_LAT * math.cos(math.radians(ref_lat))
        self._ky = _M_PER_DEG_LAT

    def to_xy(self, lon: float, lat: float) -> Tuple[float, float]:
        return lon * self._kx, lat * self._ky

    def to_lonlat(self, x: float, y: float) -> Point:
        return x / self._kx, y / self._ky


def project_point_to_segment(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> Tuple[float, float, float, float]:
    """点 P 到线段 AB 的投影。

    返回 (proj_x, proj_y, t, dist_m)，t ∈ [0,1] 为投影点在线段上的比例。
    """
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0.0:
        return ax, ay, 0.0, math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / seg_len_sq
    t = max(0.0, min(1.0, t))
    qx, qy = ax + t * dx, ay + t * dy
    return qx, qy, t, math.hypot(px - qx, py - qy)


def polyline_length_m(coords: Sequence[Point]) -> float:
    return sum(
        haversine_m(a[1], a[0], b[1], b[0]) for a, b in zip(coords, coords[1:])
    )
