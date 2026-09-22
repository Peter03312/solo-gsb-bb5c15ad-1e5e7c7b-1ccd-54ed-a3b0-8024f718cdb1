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

# 纬三路最西端附近（错开路口避免投影平局），沿边向东行驶
_WEI3_LAT, _WEI3_LON = 39.9008, 116.381 + 0.004 / 20


def _eastbound_body(name, dt_s, speed_kmh=45.0):
    """沿纬三路向东匀速行驶的轨迹（约每 85m 一个采样点）。"""
    step_m = 85.0
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = m_per_deg_lat * 0.7672  # 参考纬度 39.9°
    step_lon = step_m / m_per_deg_lon
    step_s = step_m / (speed_kmh / 3.6)
    points = [
        {
            "lat": _WEI3_LAT,
            "lon": _WEI3_LON + k * step_lon,
            "timestamp": (
                datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)
                + timedelta(seconds=dt_s + k * step_s)
            ).isoformat(),
        }
        for k in range(4)
    ]
    return {"name": name, "points": points}


def _submit(client, body):
    r = client.post("/api/trajectories", json=body)
    assert r.status_code == 201, r.text
    return r.json()["trajectory_id"]


def test_travel_profile_aggregates_edges(client):
    t1 = _submit(client, _eastbound_body("画像-行驶1", 0))
    t2 = _submit(client, _eastbound_body("画像-行驶2", 3600, speed_kmh=38.0))

    # 逗号分隔多轨迹
    r = client.get(f"/api/roads/travel-profile?trajectory_ids={t1},{t2}")
    assert r.status_code == 200
    body = r.json()
    assert set(body["trajectory_ids"]) == {t1, t2}
    assert body["missing_trajectory_ids"] == []
    assert body["unmatched_trajectory_ids"] == []
    assert body["matched_count"] == 2
    assert body["total_samples"] >= 2
    assert body["total_ignored"] == 0

    # 至少覆盖纬三路（wei3-x）且全部为向东正向
    wei_edges = [e for e in body["edges"] if e["edge_id"].startswith("wei3-")]
    assert wei_edges
    assert all(e["direction"] == "forward" for e in wei_edges)
    assert all(e["edge_name"] == "纬三路" for e in wei_edges)
    assert all(e["sample_count"] >= 1 for e in wei_edges)
    assert all(e["distance_m"] > 300 for e in wei_edges)  # 网格边约 340m
    assert all(e["median_speed_kmh"] > 0 for e in wei_edges)
    assert all(e["p90_speed_kmh"] >= e["median_speed_kmh"] for e in wei_edges)
    required = {
        "edge_id",
        "direction",
        "distance_m",
        "sample_count",
        "median_speed_kmh",
        "p90_speed_kmh",
    }
    assert required <= set(wei_edges[0])


def test_travel_profile_reports_missing_and_unmatched(client):
    good = _submit(client, _eastbound_body("画像-有效", 7200))
    # 只提交不匹配的轨迹
    r = client.post(
        "/api/trajectories",
        json={**_eastbound_body("画像-未匹配", 0), "match": False},
    )
    assert r.status_code == 201
    unmatched_tid = r.json()["trajectory_id"]

    r = client.get(
        f"/api/roads/travel-profile?trajectory_ids={good},ghost-id,{unmatched_tid}"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["trajectory_ids"] == [good]
    assert body["missing_trajectory_ids"] == ["ghost-id"]
    assert body["unmatched_trajectory_ids"] == [unmatched_tid]
    assert body["edges"]


def test_travel_profile_empty_data_not_500(client):
    # 全部 id 无效：返回空数据而不是 5xx
    r = client.get("/api/roads/travel-profile?trajectory_ids=nope1,nope2")
    assert r.status_code == 200
    body = r.json()
    assert body["edges"] == []
    assert body["ignored"] == []
    assert body["total_samples"] == 0
    assert body["total_ignored"] == 0
    assert body["matched_count"] == 0
    assert set(body["missing_trajectory_ids"]) == {"nope1", "nope2"}

    # 完全不传 query 参数 -> 422；传空值同样 422
    assert client.get("/api/roads/travel-profile").status_code == 422
    assert client.get("/api/roads/travel-profile?trajectory_ids=").status_code == 422


def test_travel_profile_ignores_bad_samples(client):
    # 1) 无时间戳：全部样本被 missing_timestamp 剔除
    no_ts_body = _eastbound_body("画像-无时间戳", 0)
    for p in no_ts_body["points"]:
        p.pop("timestamp")
    t_no_ts = _submit(client, no_ts_body)

    # 2) 时间戳逆序：non_monotonic_timestamp
    rev_body = _eastbound_body("画像-时间逆序", 0)
    rev_body["points"] = list(reversed(rev_body["points"]))
    t_rev = _submit(client, rev_body)

    r = client.get(f"/api/roads/travel-profile?trajectory_ids={t_no_ts},{t_rev}")
    assert r.status_code == 200
    body = r.json()
    assert body["edges"] == []
    assert body["total_samples"] == 0
    reasons = body["ignored_count_by_reason"]
    assert reasons.get("missing_timestamp", 0) >= 1
    assert reasons.get("non_monotonic_timestamp", 0) >= 1
    assert body["total_ignored"] >= 2
    rec = body["ignored"][0]
    assert {"trajectory_id", "seq_from", "seq_to", "reason", "detail"} <= set(rec)
    assert rec["trajectory_id"] in {t_no_ts, t_rev}
