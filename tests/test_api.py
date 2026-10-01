"""API 端到端测试。"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def sample_trajectory():
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "examples",
        "sample_trajectory.json",
    )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["network"]["edge_count"] == 144


def test_submit_and_match_flow(client, sample_trajectory):
    # 提交轨迹（自动匹配）
    r = client.post("/api/trajectories", json=sample_trajectory)
    assert r.status_code == 201
    created = r.json()
    assert created["matched"] is True
    assert created["match_status"] == "matched"
    tid = created["trajectory_id"]

    # 查询轨迹
    r = client.get(f"/api/trajectories/{tid}")
    assert r.status_code == 200
    assert r.json()["point_count"] == len(sample_trajectory["points"])

    # 查询匹配结果（含逐点明细）
    r = client.get(f"/api/trajectories/{tid}/match")
    assert r.status_code == 200
    match = r.json()
    assert match["status"] == "matched"
    result = match["result"]
    assert result["matched_count"] == result["point_count"]
    p0 = result["points"][0]
    assert {"edge_id", "edge_name", "distance_m", "emission_logp", "projected"} <= set(p0)
    # 中间点应有转移概率明细
    mid = result["points"][result["point_count"] // 2]
    assert mid["transition"]["route_distance_m"] > 0
    assert mid["transition"]["implied_speed_kmh"] is not None

    # GeoJSON 导出
    r = client.get(f"/api/trajectories/{tid}/match/geojson")
    assert r.status_code == 200
    gj = r.json()
    assert gj["type"] == "FeatureCollection"
    kinds = {f["properties"]["kind"] for f in gj["features"]}
    assert "matched_path" in kinds and "matched_point" in kinds
    path_features = [f for f in gj["features"] if f["properties"]["kind"] == "matched_path"]
    assert path_features[0]["geometry"]["type"] == "LineString"

    # 重新匹配（自定义参数）
    r = client.post(f"/api/trajectories/{tid}/match", json={"radius_m": 80, "sigma_m": 20})
    assert r.status_code == 200
    assert r.json()["status"] == "matched"


def test_break_reason_exposed(client):
    body = {
        "name": "含离网点轨迹",
        "points": [
            {"lat": 39.9008, "lon": 116.381, "timestamp": "2026-09-21T08:00:00Z"},
            {"lat": 39.96, "lon": 116.381, "timestamp": "2026-09-21T08:00:30Z"},
            {"lat": 39.9008, "lon": 116.383, "timestamp": "2026-09-21T08:01:00Z"},
        ],
    }
    r = client.post("/api/trajectories", json=body)
    assert r.status_code == 201
    tid = r.json()["trajectory_id"]
    r = client.get(f"/api/trajectories/{tid}/match")
    result = r.json()["result"]
    assert result["status"] == "partial"
    reasons = {b["reason"] for b in result["breaks"]}
    assert "no_candidate_within_radius" in reasons


def test_404s(client):
    assert client.get("/api/trajectories/nope").status_code == 404
    assert client.get("/api/trajectories/nope/match").status_code == 404
    assert client.post("/api/trajectories/nope/match").status_code == 404


# ---------- 道路通行画像 ----------

CENTER_LON, CENTER_LAT = 116.397, 39.908
DLON = 0.004
LAT_WEI3 = CENTER_LAT + (2 - 4) * 0.0036
T0 = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)


def _submit(client, name, points):
    r = client.post("/api/trajectories", json={"name": name, "points": points})
    assert r.status_code == 201, r.text
    return r.json()["trajectory_id"]


def _eastbound_points(step_s=9):
    """沿纬三路向东，约 100m/点（~40km/h），覆盖 wei3-1 / wei3-2 两条边。"""
    start_lon = CENTER_LON - 4 * DLON + 0.0002
    return [
        {
            "lat": LAT_WEI3,
            "lon": start_lon + k * 0.001167,
            "timestamp": (T0 + timedelta(seconds=step_s * k)).isoformat(),
        }
        for k in range(8)
    ]


def _westbound_points(step_s=9):
    east = _eastbound_points(step_s)
    pts = list(reversed(east))
    for k, p in enumerate(pts):
        p["timestamp"] = (T0 + timedelta(seconds=step_s * k)).isoformat()
    return pts


def test_travel_profile_empty_inputs_are_200(client):
    # 缺参数：空数据而非 4xx/5xx
    r = client.get("/api/roads/travel-profile")
    assert r.status_code == 200
    body = r.json()
    assert body["edges"] == []
    assert body["trajectory_ids"] == []
    assert body["trajectory_count"] == 0

    # 不存在 / 未匹配的轨迹：跳过并说明，仍返回 200
    r = client.get("/api/roads/travel-profile?trajectory_ids=does-not-exist")
    assert r.status_code == 200
    body = r.json()
    assert body["edges"] == []
    assert body["trajectory_count"] == 0
    assert body["ignored_trajectories"] == [
        {
            "trajectory_id": "does-not-exist",
            "reason": "match_not_found",
            "detail": "轨迹不存在或尚未匹配，已跳过",
        }
    ]

    # 提交但未匹配（match=false）的轨迹同样跳过
    r = client.post(
        "/api/trajectories",
        json={"name": "未匹配轨迹", "points": _eastbound_points(), "match": False},
    )
    tid = r.json()["trajectory_id"]
    r = client.get(f"/api/roads/travel-profile?trajectory_ids={tid}")
    assert r.status_code == 200
    assert r.json()["edges"] == []
    assert r.json()["ignored_trajectories"][0]["trajectory_id"] == tid


def test_travel_profile_aggregates_by_edge_and_direction(client):
    east_id = _submit(client, "东向通行", _eastbound_points())
    west_id = _submit(client, "西向通行", _westbound_points())

    r = client.get(f"/api/roads/travel-profile?trajectory_ids={east_id},{west_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["trajectory_ids"] == [east_id, west_id]
    assert body["trajectory_count"] == 2
    assert body["ignored_sample_count"] == 0
    assert body["ignored_samples"] == []
    assert body["ignored_trajectories"] == []

    edges = body["edges"]
    assert edges, "应至少聚合出纬三路的两条边"
    assert {e["edge_name"] for e in edges} == {"纬三路"}
    directions = {e["direction"] for e in edges}
    assert directions == {"forward", "backward"}
    for e in edges:
        assert {"sample_count", "direction", "distance_m", "median_speed_kmh",
                "p90_speed_kmh"} <= set(e)
        assert e["sample_count"] >= 1
        assert e["distance_m"] > 0
        assert e["edge_length_m"] > 300  # 网格边长约 342m
        # ~40km/h 的匀速轨迹
        assert 30 < e["median_speed_kmh"] < 50
        assert e["p90_speed_kmh"] >= e["median_speed_kmh"]

    # 仅查东向轨迹时只有 forward；8 点 = 7 对转移（跨路口的转移在两条边上各留一个样本）
    r = client.get(f"/api/roads/travel-profile?trajectory_ids={east_id}")
    body = r.json()
    east_edges = body["edges"]
    assert {e["direction"] for e in east_edges} == {"forward"}
    assert body["transition_count"] == 7
    assert sum(e["sample_count"] for e in east_edges) >= 7


def test_travel_profile_reports_ignored_samples(client):
    # 无时间戳：可正常匹配，但无法测速 -> missing_timestamp
    no_ts = [{"lat": p["lat"], "lon": p["lon"]} for p in _eastbound_points()[:3]]
    no_ts_id = _submit(client, "无时间戳", no_ts)

    # 时间戳逆序：lon 向东但时间往回走
    rev = _eastbound_points()
    for k, p in enumerate(rev):
        p["timestamp"] = (T0 - timedelta(seconds=9 * k)).isoformat()
    rev_id = _submit(client, "时间逆序", rev)

    r = client.get(
        f"/api/roads/travel-profile?trajectory_ids={no_ts_id},{rev_id}"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["edges"] == []
    reasons = {s["reason"] for s in body["ignored_samples"]}
    assert "missing_timestamp" in reasons
    assert "timestamp_reversed" in reasons
    for s in body["ignored_samples"]:
        assert s["trajectory_id"] in {no_ts_id, rev_id}
        assert s["seq"] is not None
        assert s["detail"]

