#!/usr/bin/env python3
"""生成内置示例路网（9x9 网格城市，路口已打断）与一条示例轨迹。

用法: python3 scripts/generate_roads.py
输出: app/data/roads.geojson, examples/sample_trajectory.json
"""
import json
import math
import os
import random
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CENTER_LON, CENTER_LAT = 116.397, 39.908
N = 9                      # 每方向 9 条街
DLON, DLAT = 0.004, 0.0036  # 网格间距（约 340m x 400m）
CN_NUM = "一二三四五六七八九"


def lon_at(i: int) -> float:
    return CENTER_LON + (i - (N - 1) / 2) * DLON


def lat_at(j: int) -> float:
    return CENTER_LAT + (j - (N - 1) / 2) * DLAT


def build_roads() -> dict:
    features = []

    def add(fid, name, coords, maxspeed):
        features.append(
            {
                "type": "Feature",
                "properties": {"id": fid, "name": name, "maxspeed": maxspeed},
                "geometry": {"type": "LineString", "coordinates": coords},
            }
        )

    # 东西向：纬一路(南) ~ 纬九路(北)
    for j in range(N):
        name = f"纬{CN_NUM[j]}路"
        speed = 60 if j == 4 else 40  # 纬五路为主干道
        lat = lat_at(j)
        for i in range(N - 1):
            add(
                f"wei{j + 1}-{i + 1}",
                name,
                [[round(lon_at(i), 6), round(lat, 6)], [round(lon_at(i + 1), 6), round(lat, 6)]],
                speed,
            )
    # 南北向：经一路(西) ~ 经九路(东)
    for i in range(N):
        name = f"经{CN_NUM[i]}路"
        speed = 60 if i == 4 else 40  # 经五路为主干道
        lon = lon_at(i)
        for j in range(N - 1):
            add(
                f"jing{i + 1}-{j + 1}",
                name,
                [[round(lon, 6), round(lat_at(j), 6)], [round(lon, 6), round(lat_at(j + 1), 6)]],
                speed,
            )
    return {"type": "FeatureCollection", "features": features}


def build_sample_trajectory() -> dict:
    """沿 纬三路向东 -> 经五路向北 -> 纬七路向东，带高斯噪声与固定采样间隔。"""
    rng = random.Random(42)
    # 折线途径的网格交叉点 (i, j)
    waypoints = [(0, 2), (4, 2), (4, 6), (8, 6)]
    step_m = 90.0
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = m_per_deg_lat * math.cos(math.radians(CENTER_LAT))

    raw = []
    for (i0, j0), (i1, j1) in zip(waypoints, waypoints[1:]):
        x0, y0 = lon_at(i0), lat_at(j0)
        x1, y1 = lon_at(i1), lat_at(j1)
        seg_m = math.hypot((x1 - x0) * m_per_deg_lon, (y1 - y0) * m_per_deg_lat)
        steps = max(1, int(seg_m / step_m))
        for k in range(steps):
            t = k / steps
            raw.append((x0 + t * (x1 - x0), y0 + t * (y1 - y0)))
    raw.append((lon_at(waypoints[-1][0]), lat_at(waypoints[-1][1])))

    t0 = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)
    points = []
    for k, (lon, lat) in enumerate(raw):
        points.append(
            {
                "lat": round(lat + rng.gauss(0, 8) / m_per_deg_lat, 6),
                "lon": round(lon + rng.gauss(0, 8) / m_per_deg_lon, 6),
                "timestamp": (t0 + timedelta(seconds=8 * k)).isoformat(),
            }
        )
    return {"name": "示例轨迹-早高峰通勤", "points": points}


def main():
    roads = build_roads()
    roads_path = os.path.join(ROOT, "app", "data", "roads.geojson")
    with open(roads_path, "w", encoding="utf-8") as f:
        json.dump(roads, f, ensure_ascii=False, indent=1)
    print(f"roads: {len(roads['features'])} features -> {roads_path}")

    traj = build_sample_trajectory()
    traj_path = os.path.join(ROOT, "examples", "sample_trajectory.json")
    with open(traj_path, "w", encoding="utf-8") as f:
        json.dump(traj, f, ensure_ascii=False, indent=1)
    print(f"trajectory: {len(traj['points'])} points -> {traj_path}")


if __name__ == "__main__":
    main()
