"""匹配核心：候选投影 + Viterbi 解码的单元测试。"""
from datetime import datetime, timedelta, timezone

from app.mapmatch.matcher import GpsPoint, MatchParams, match_trajectory

# 纬三路是 j=2 的东西向街道（见 scripts/generate_roads.py）
CENTER_LON, CENTER_LAT = 116.397, 39.908
DLON, DLAT = 0.004, 0.0036


def lat_at(j):
    return CENTER_LAT + (j - 4) * DLAT


def lon_at(i):
    return CENTER_LON + (i - 4) * DLON


def test_straight_trajectory_matches_correct_road(network):
    """沿纬三路的无噪声轨迹应全部匹配到纬三路（采样点错开路口避免平局）。"""
    t0 = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)
    points = [
        GpsPoint(
            lat=lat_at(2),
            lon=lon_at(0) + DLON / 20 + k * DLON / 10,
            timestamp=t0 + timedelta(seconds=30 * k),
        )
        for k in range(20)
    ]
    result = match_trajectory(network, points, MatchParams())
    assert result["status"] == "matched"
    assert result["matched_count"] == 20
    assert all(p["edge_name"] == "纬三路" for p in result["points"])
    assert result["breaks"] == []
    assert len(result["segments"]) == 1
    assert len(result["segments"][0]["geometry"]) >= 2


def test_turn_trajectory_uses_connected_roads(network):
    """转弯轨迹应匹配到两条相连的道路。"""
    t0 = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)
    pts = []
    # 沿纬三路向东直到经五路（采样点错开路口）
    for k in range(21):
        pts.append(
            GpsPoint(
                lat=lat_at(2),
                lon=lon_at(0) + DLON / 20 + k * DLON / 5,
                timestamp=t0 + timedelta(seconds=30 * k),
            )
        )
    # 沿经五路向北
    for k in range(1, 11):
        pts.append(
            GpsPoint(
                lat=lat_at(2) + DLAT / 20 + k * DLAT / 5,
                lon=lon_at(4),
                timestamp=t0 + timedelta(seconds=30 * (20 + k)),
            )
        )
    result = match_trajectory(network, pts, MatchParams())
    assert result["status"] == "matched"
    names = {p["edge_name"] for p in result["points"]}
    assert names == {"纬三路", "经五路"}


def test_off_network_point_causes_break(network):
    """远离路网的点应产生 no_candidate_within_radius 断段。"""
    t0 = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)
    points = [
        GpsPoint(lat=lat_at(2), lon=lon_at(0), timestamp=t0),
        GpsPoint(lat=lat_at(2) + 0.05, lon=lon_at(0), timestamp=t0 + timedelta(seconds=30)),  # 北约5.5km
        GpsPoint(lat=lat_at(2), lon=lon_at(0) + DLON / 2, timestamp=t0 + timedelta(seconds=60)),
    ]
    result = match_trajectory(network, points, MatchParams())
    assert result["status"] == "partial"
    assert result["matched_count"] == 2
    assert any(b["reason"] == "no_candidate_within_radius" and b["seq"] == 1 for b in result["breaks"])


def test_impossible_speed_causes_break(network):
    """时间戳间隔过短导致隐含速度超过硬上限时，转移不可行并断段。"""
    t0 = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)
    points = [
        GpsPoint(lat=lat_at(2), lon=lon_at(0), timestamp=t0),
        # 1 秒后出现在约 680m 外 -> 隐含速度 ~2400km/h
        GpsPoint(lat=lat_at(2), lon=lon_at(2), timestamp=t0 + timedelta(seconds=1)),
    ]
    result = match_trajectory(network, points, MatchParams())
    assert result["status"] == "partial"
    assert result["matched_count"] == 1
    assert any(b["reason"] == "no_feasible_transition" for b in result["breaks"])


def test_candidates_sorted_by_distance(network):
    cands = network.candidates(lon_at(4) + 0.0001, lat_at(4), 50)
    assert cands
    dists = [c.dist_m for c in cands]
    assert dists == sorted(dists)
    assert dists[0] < 20  # 距经五路/纬五路交叉口很近
