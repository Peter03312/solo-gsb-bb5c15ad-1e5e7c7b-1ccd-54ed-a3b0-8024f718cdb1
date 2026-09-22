"""道路通行画像聚合的单元测试。"""
from datetime import datetime, timedelta, timezone

from app.mapmatch.travel_profile import build_travel_profile


def _pt(seq, eid, along, ts, leg=None):
    """构造匹配结果 points 数组中的一个点（只保留画像所需字段）。"""
    return {
        "seq": seq,
        "timestamp": ts.isoformat() if ts is not None else None,
        "edge_id": eid,
        "transition": None if leg is None else {"legs": [leg]},
    }


T0 = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)


def test_profile_aggregates_median_and_p90(network):
    """同一条边同一方向的多次行驶应聚合出中位数与 P90 速度。"""
    eid = "wei3-1"  # 纬三路最西段，长度约 340m，正向向东
    length = network.edges[eid].length_m

    # 10s 走完全程 -> 36km/h 起步，后续给出递增速度样本
    def run(kmh, idx):
        dur = length / (kmh / 3.6)
        ts0 = T0 + timedelta(minutes=idx * 10)
        return [
            _pt(0, eid, 0.0, ts0),
            _pt(1, eid, length, ts0 + timedelta(seconds=dur), leg=(eid, 0.0, length)),
        ]

    matches = [(f"t{i}", {"points": run(kmh, i)}) for i, kmh in enumerate(range(30, 40))]
    profile = build_travel_profile(network, matches)

    keys = {(e["edge_id"], e["direction"]) for e in profile["edges"]}
    assert (eid, "forward") in keys
    item = next(e for e in profile["edges"] if e["edge_id"] == eid)
    assert item["sample_count"] == 10
    assert item["direction"] == "forward"
    assert abs(item["distance_m"] - length) < 0.2
    assert item["median_speed_kmh"] == 34.5  # 30..39 的中位数
    assert item["p90_speed_kmh"] >= 38.0
    assert profile["total_ignored"] == 0
    assert profile["ignored_count_by_reason"] == {}


def test_profile_separates_directions(network):
    """同一条边的正向与反向样本应分别成组。"""
    eid = "wei3-1"
    length = network.edges[eid].length_m
    fwd = {
        "points": [
            _pt(0, eid, 0.0, T0),
            _pt(1, eid, length, T0 + timedelta(seconds=17), leg=(eid, 0.0, length)),
        ]
    }
    bwd = {
        "points": [
            _pt(0, eid, length, T0 + timedelta(hours=1)),
            _pt(
                1,
                eid,
                0.0,
                T0 + timedelta(hours=1, seconds=13),
                leg=(eid, length, 0.0),
            ),
        ]
    }
    profile = build_travel_profile(network, [("f", fwd), ("b", bwd)])
    by_dir = {e["direction"]: e for e in profile["edges"] if e["edge_id"] == eid}
    assert set(by_dir) == {"forward", "backward"}
    assert by_dir["forward"]["sample_count"] == 1
    assert by_dir["backward"]["sample_count"] == 1


def test_profile_multi_edge_legs_split_time_by_distance(network):
    """跨边 legs 应按里程占比分摊时间，两个方向的两条边各得一个样本。"""
    e1, e2 = "wei3-1", "wei3-2"
    l1, l2 = network.edges[e1].length_m, network.edges[e2].length_m
    total = l1 + l2
    dur = total / (40 / 3.6)
    result = {
        "points": [
            _pt(0, e1, 0.0, T0),
            _pt(
                1,
                e2,
                l2,
                T0 + timedelta(seconds=dur),
                leg=None,
            ),
        ]
    }
    # 真实结果中转移明细带两条 legs
    result["points"][1]["transition"] = {
        "legs": [(e1, 0.0, l1), (e2, 0.0, l2)]
    }
    profile = build_travel_profile(network, [("t", result)])
    assert profile["total_samples"] == 2
    speeds = {e["edge_id"]: e["median_speed_kmh"] for e in profile["edges"]}
    assert abs(speeds[e1] - 40.0) < 0.2
    assert abs(speeds[e2] - 40.0) < 0.2


def test_profile_reports_ignore_reasons(network):
    """时间戳缺失/逆序/跨断段/零距离/异常速度等样本应被剔除并说明原因。"""
    eid = "wei3-1"
    length = network.edges[eid].length_m

    def pair(a, b):
        return {"points": [a, b]}

    cases = [
        # 缺时间戳
        ("t-missing", pair(_pt(0, eid, 0.0, None),
                           _pt(1, eid, length, None, leg=(eid, 0.0, length)))),
        # 逆序
        ("t-reverse", pair(_pt(0, eid, 0.0, T0 + timedelta(seconds=30)),
                           _pt(1, eid, length, T0 + timedelta(seconds=20),
                               leg=(eid, 0.0, length)))),
        # 等时间戳（非单调的另一种形式）
        ("t-zero-ts", pair(_pt(0, eid, 0.0, T0),
                           _pt(1, eid, length, T0, leg=(eid, 0.0, length)))),
        # 序号不连贯（跨断段）
        ("t-gap", pair(_pt(4, eid, 0.0, T0),
                       _pt(6, eid, length, T0 + timedelta(seconds=20),
                           leg=(eid, 0.0, length)))),
        # 缺少转移明细
        ("t-no-legs", pair(_pt(0, eid, 0.0, T0),
                           _pt(1, eid, length, T0 + timedelta(seconds=20)))),
        # 零距离
        ("t-zero-dist", pair(_pt(0, eid, 50.0, T0),
                             _pt(1, eid, 50.0, T0 + timedelta(seconds=10),
                                 leg=(eid, 50.0, 50.0)))),
        # ~1200km/h 异常速度
        ("t-fast", pair(_pt(0, eid, 0.0, T0),
                        _pt(1, eid, length, T0 + timedelta(seconds=1),
                            leg=(eid, 0.0, length)))),
    ]
    profile = build_travel_profile(network, cases)
    reasons = profile["ignored_count_by_reason"]
    assert reasons == {
        "missing_timestamp": 1,
        "non_monotonic_timestamp": 2,
        "incoherent_match": 2,
        "zero_distance": 1,
        "implausible_speed": 1,
    }
    assert profile["total_ignored"] == 7
    assert {(r["trajectory_id"], r["seq_from"], r["seq_to"]) for r in profile["ignored"]} == {
        ("t-missing", 0, 1),
        ("t-reverse", 0, 1),
        ("t-zero-ts", 0, 1),
        ("t-gap", 4, 6),
        ("t-no-legs", 0, 1),
        ("t-zero-dist", 0, 1),
        ("t-fast", 0, 1),
    }
    assert profile["edges"] == []
    assert profile["total_samples"] == 0


def test_profile_edge_not_found(network):
    """引用当前路网中不存在的边时记录 edge_not_found，其余边样本仍可用。"""
    good = "wei3-1"
    length = network.edges[good].length_m
    result = {
        "points": [
            _pt(0, "ghost", 0.0, T0),
            _pt(
                1,
                good,
                length,
                T0 + timedelta(seconds=20),
            ),
        ]
    }
    result["points"][1]["transition"] = {"legs": [("ghost", 0.0, 100.0), (good, 0.0, length)]}
    profile = build_travel_profile(network, [("t", result)])
    assert profile["ignored_count_by_reason"].get("edge_not_found") == 1
    rec = next(r for r in profile["ignored"] if r["reason"] == "edge_not_found")
    assert rec["edge_id"] == "ghost"
    # good 边样本按剩余里程与全程时间之比计算，仍然产出
    assert any(e["edge_id"] == good for e in profile["edges"])


def test_profile_empty_matches_returns_empty(network):
    profile = build_travel_profile(network, [])
    assert profile == {
        "edges": [],
        "ignored": [],
        "ignored_count_by_reason": {},
        "total_samples": 0,
        "total_ignored": 0,
    }
