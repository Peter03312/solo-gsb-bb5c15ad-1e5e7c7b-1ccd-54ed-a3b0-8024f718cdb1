"""道路通行画像聚合的单元测试。"""
from datetime import datetime, timedelta, timezone

from app.mapmatch.matcher import GpsPoint, MatchParams, match_trajectory
from app.mapmatch.travel_profile import (
    DIRECTION_BACKWARD,
    DIRECTION_FORWARD,
    REASON_MISSING_TIMESTAMP,
    REASON_NOT_CONTIGUOUS,
    REASON_TIMESTAMP_NOT_INCREASING,
    REASON_TIMESTAMP_REVERSED,
    REASON_ZERO_DISTANCE,
    build_travel_profile,
    percentile,
)

T0 = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)


def _point(seq, edge_id, timestamp=None, legs=None):
    """构造持久化形态的逐点匹配结果（legs 用 list 以模拟 JSON 往返）。"""
    return {
        "seq": seq,
        "timestamp": timestamp,
        "edge_id": edge_id,
        "edge_name": edge_id,
        "along_m": 0.0,
        "projected": {"lon": 0.0, "lat": 0.0},
        "transition": {"legs": legs} if legs is not None else None,
    }


def _result(points):
    return {"status": "matched", "points": points}


def test_percentile_linear_interpolation():
    assert percentile([36.0], 0.9) == 36.0
    assert percentile([36.0, 72.0], 0.5) == 54.0
    assert percentile([36.0, 72.0], 0.9) == 68.4
    vals = [10.0, 20.0, 30.0, 40.0]
    assert percentile(vals, 0.0) == 10.0
    assert percentile(vals, 1.0) == 40.0


def test_empty_input_returns_empty_edges():
    profile = build_travel_profile([])
    assert profile["edges"] == []
    assert profile["ignored_samples"] == []
    assert profile["trajectory_count"] == 0
    assert profile["transition_count"] == 0


def test_single_edge_forward_speed():
    """100m / 10s = 36km/h，中位数与 P90 相同。"""
    pts = [
        _point(0, "e1", T0.isoformat()),
        _point(1, "e1", (T0 + timedelta(seconds=10)).isoformat(), [["e1", 0.0, 100.0]]),
    ]
    profile = build_travel_profile([("t1", _result(pts))])
    assert len(profile["edges"]) == 1
    edge = profile["edges"][0]
    assert edge["edge_id"] == "e1"
    assert edge["direction"] == DIRECTION_FORWARD
    assert edge["sample_count"] == 1
    assert edge["distance_m"] == 100.0
    assert edge["duration_s"] == 10.0
    assert edge["median_speed_kmh"] == 36.0
    assert edge["p90_speed_kmh"] == 36.0


def test_backward_detection_and_median_p90():
    """to_m < from_m 为反向；两样本 36/72km/h -> median 54, p90 68.4。"""
    pts = [
        _point(0, "e1", T0.isoformat()),
        _point(1, "e1", (T0 + timedelta(seconds=10)).isoformat(), [["e1", 100.0, 0.0]]),
        _point(2, "e1", (T0 + timedelta(seconds=15)).isoformat(), [["e1", 100.0, 0.0]]),
    ]
    profile = build_travel_profile([("t1", _result(pts))])
    edge = profile["edges"][0]
    assert edge["direction"] == DIRECTION_BACKWARD
    assert edge["sample_count"] == 2
    assert edge["median_speed_kmh"] == 54.0
    assert edge["p90_speed_kmh"] == 68.4
    assert profile["ignored_sample_count"] == 0


def test_multiple_edges_split_time_by_distance():
    """途经 e1(100m) + e2(300m)，总时长 20s（=72km/h），按里程占比分摊。"""
    pts = [
        _point(0, "e1", T0.isoformat()),
        _point(
            1,
            "e2",
            (T0 + timedelta(seconds=20)).isoformat(),
            [["e1", 0.0, 100.0], ["e2", 0.0, 300.0]],
        ),
    ]
    profile = build_travel_profile([("t1", _result(pts))])
    by_edge = {e["edge_id"]: e for e in profile["edges"]}
    assert by_edge["e1"]["sample_count"] == 1
    assert by_edge["e1"]["duration_s"] == 5.0
    assert by_edge["e1"]["median_speed_kmh"] == 72.0
    assert by_edge["e2"]["duration_s"] == 15.0
    assert by_edge["e2"]["distance_m"] == 300.0
    assert by_edge["e2"]["median_speed_kmh"] == 72.0


def test_ignored_reasons():
    t = lambda s: (T0 + timedelta(seconds=s)).isoformat()
    cases = [
        # 缺时间戳
        ([_point(0, "e1", None), _point(1, "e1", t(10), [["e1", 0, 100]])],
         REASON_MISSING_TIMESTAMP),
        # 时间戳逆序
        ([_point(0, "e1", t(10)), _point(1, "e1", t(0), [["e1", 0, 100]])],
         REASON_TIMESTAMP_REVERSED),
        # 时间戳相同
        ([_point(0, "e1", t(5)), _point(1, "e1", t(5), [["e1", 0, 100]])],
         REASON_TIMESTAMP_NOT_INCREASING),
        # 断段后首个点没有 legs（不连贯）
        ([_point(0, "e1", t(0)), _point(2, "e2", t(10), None)],
         REASON_NOT_CONTIGUOUS),
        # 沿网距离为 0
        ([_point(0, "e1", t(0)), _point(1, "e1", t(10), [["e1", 50.0, 50.0]])],
         REASON_ZERO_DISTANCE),
    ]
    for i, (pts, reason) in enumerate(cases):
        profile = build_travel_profile([(f"t{i}", _result(pts))])
        assert profile["edges"] == [], f"case {reason} 不应产出边样本"
        assert len(profile["ignored_samples"]) == 1
        assert profile["ignored_samples"][0]["reason"] == reason

    # 未匹配点（None）打断连贯性：其两侧不构成样本对
    pts = [
        _point(0, "e1", t(0)),
        None,
        _point(2, "e1", t(20), None),
    ]
    profile = build_travel_profile([("tn", _result(pts))])
    assert profile["edges"] == []
    assert profile["ignored_samples"] == []
    assert profile["transition_count"] == 0


def test_zero_length_legs_are_skipped_but_pair_kept():
    """投影恰好在节点上产生的 0 长度腿应忽略，其余腿仍为有效样本。"""
    pts = [
        _point(0, "e1", T0.isoformat()),
        _point(
            1,
            "e2",
            (T0 + timedelta(seconds=10)).isoformat(),
            [["e1", 0.0, 0.0], ["e2", 0.0, 100.0]],
        ),
    ]
    profile = build_travel_profile([("t1", _result(pts))])
    assert {e["edge_id"] for e in profile["edges"]} == {"e2"}
    assert profile["ignored_sample_count"] == 0


def test_network_enriches_edge_metadata(network):
    pts = [
        _point(0, "wei3-1", T0.isoformat()),
        _point(
            1,
            "wei3-1",
            (T0 + timedelta(seconds=9)).isoformat(),
            [["wei3-1", 0.0, 100.0]],
        ),
    ]
    profile = build_travel_profile([("t1", _result(pts))], network=network)
    edge = profile["edges"][0]
    assert edge["edge_name"] == "纬三路"
    assert edge["edge_length_m"] > 300  # 网格边长约 342m


def test_end_to_end_with_matcher(network):
    """沿纬三路向东的真实匹配结果应产出 forward 方向画像。"""
    from tests.test_matcher import lat_at, lon_at

    t0 = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)
    points = [
        GpsPoint(
            lat=lat_at(2),
            lon=lon_at(0) + 0.0002 + k * 0.00117,  # 约 100m 一点 = 40km/h @9s
            timestamp=t0 + timedelta(seconds=9 * k),
        )
        for k in range(8)
    ]
    result = match_trajectory(network, points, MatchParams())
    assert result["matched_count"] == 8
    profile = build_travel_profile([("t1", result)], network=network)
    assert profile["edges"]
    assert all(e["direction"] == DIRECTION_FORWARD for e in profile["edges"])
    assert {e["edge_name"] for e in profile["edges"]} == {"纬三路"}
    edge = next(e for e in profile["edges"] if e["sample_count"])
    assert 30 < edge["median_speed_kmh"] < 50
    assert edge["p90_speed_kmh"] >= edge["median_speed_kmh"]
