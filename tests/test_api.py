"""API 端到端测试。"""
import json
import os

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
