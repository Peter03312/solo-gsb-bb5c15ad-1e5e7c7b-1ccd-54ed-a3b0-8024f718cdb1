"""道路通行画像：基于已持久化的匹配结果聚合沿网通行速度。

对每条轨迹匹配结果中**相邻的匹配点**：
- 取其 Viterbi 转移明细 ``transition.legs``（沿网途经的边及里程区间），
  按边与行驶方向（forward/backward）切分样本；
- 两点间时间按各边里程占比分摊（假设区间内匀速），得到每条边上的隐含速度；
- 同一 (edge, direction) 聚合，用中位数表示典型速度、P90 表示高峰速度。

无法使用的相邻点会计入 ignored，原因包括：
- ``missing_timestamp``        任一端时间戳缺失
- ``non_monotonic_timestamp``  时间戳逆序或时间差为 0
- ``incoherent_match``         转移明细缺失/无 legs 或序号不连贯
- ``edge_not_found``           匹配结果引用了当前路网中不存在的边
- ``zero_distance``            整个相邻对的沿网里程为 0
- ``implausible_speed``        隐含速度超过可信上限（默认 180km/h）
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from .road_network import RoadNetwork

DEFAULT_PROFILE_MAX_SPEED_KMH = 180.0
_MIN_DIST_M = 1e-6

# 忽略原因 -> 说明（供 API 文档/响应复用）
IGNORE_REASONS: Dict[str, str] = {
    "missing_timestamp": "相邻匹配点至少一端缺少时间戳，无法计算通行时间",
    "non_monotonic_timestamp": "时间戳逆序或时间差为 0，行驶方向与时间不一致",
    "incoherent_match": "匹配点不连贯：缺少转移明细/legs 或序号不连续（跨断段样本）",
    "edge_not_found": "匹配结果引用的边在当前路网中不存在",
    "zero_distance": "相邻匹配点间沿网距离为 0，无法计算速度",
    "implausible_speed": "隐含速度超过可信上限，视为异常 GPS 样本",
}


def _parse_timestamp(value) -> Optional[datetime]:
    """解析匹配结果中的 ISO8601 时间；naive 时间按 UTC 处理。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        ts = value
    else:
        try:
            ts = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _percentile(sorted_values: Sequence[float], p: float) -> float:
    """线性插值百分位数，p ∈ [0, 1]，入参需已升序。"""
    if not sorted_values:
        raise ValueError("空样本无法计算百分位")
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def _pair_ignored(
    trajectory_id: str,
    seq_a: int,
    seq_b: int,
    reason: str,
    edge_ids: Optional[List[str]] = None,
    detail: Optional[str] = None,
    edge_id: Optional[str] = None,
) -> dict:
    return {
        "trajectory_id": trajectory_id,
        "seq_from": seq_a,
        "seq_to": seq_b,
        "edge_id": edge_id,
        "edge_ids": edge_ids or [],
        "reason": reason,
        "detail": detail or IGNORE_REASONS.get(reason),
    }


def build_travel_profile(
    network: RoadNetwork,
    matches: Sequence[Tuple[str, dict]],
    max_speed_kmh: float = DEFAULT_PROFILE_MAX_SPEED_KMH,
) -> dict:
    """聚合多条轨迹的匹配结果为道路通行画像。

    参数:
        network:  当前路网（用于取边长、校验 edge_id）
        matches:  [(trajectory_id, match_result), ...]，match_result 为
                  ``GET /api/trajectories/{id}/match`` 中 result 部分
        max_speed_kmh: 可信速度上限，超过的边样本计入 implausible_speed

    返回:
        ``{"edges": [...], "ignored": [...], "ignored_count_by_reason": {...},
           "total_samples": n, "total_ignored": m}``
    """
    speeds: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    sampled_dist: Dict[Tuple[str, str], float] = defaultdict(float)
    sampled_dur: Dict[Tuple[str, str], float] = defaultdict(float)
    ignored: List[dict] = []

    for trajectory_id, result in matches:
        points = (result or {}).get("points") or []
        for i in range(len(points) - 1):
            a, b = points[i], points[i + 1]
            if a is None or b is None:
                # 断段两侧的点不是“相邻匹配点”，不构成样本也不计入忽略
                continue
            seq_a, seq_b = a.get("seq", i), b.get("seq", i + 1)
            raw_legs = (b.get("transition") or {}).get("legs") or []
            edge_ids = [leg[0] for leg in raw_legs if isinstance(leg, (list, tuple)) and leg]

            # 1) 时间戳校验：缺失 / 逆序
            ts_a = _parse_timestamp(a.get("timestamp"))
            ts_b = _parse_timestamp(b.get("timestamp"))
            if ts_a is None or ts_b is None:
                ignored.append(
                    _pair_ignored(
                        trajectory_id, seq_a, seq_b, "missing_timestamp", edge_ids=edge_ids
                    )
                )
                continue
            dt_s = (ts_b - ts_a).total_seconds()
            if dt_s <= 0:
                detail = (
                    "两个匹配点时间戳完全相同"
                    if dt_s == 0
                    else f"时间戳逆序（时间差 {dt_s:.0f}s）"
                )
                ignored.append(
                    _pair_ignored(
                        trajectory_id,
                        seq_a,
                        seq_b,
                        "non_monotonic_timestamp",
                        edge_ids=edge_ids,
                        detail=detail,
                    )
                )
                continue

            # 2) 连贯性校验：必须有沿网 legs，且序号连续
            if seq_b != seq_a + 1 or not raw_legs:
                ignored.append(
                    _pair_ignored(
                        trajectory_id, seq_a, seq_b, "incoherent_match", edge_ids=edge_ids
                    )
                )
                continue

            # 3) 拆解为逐边样本（跳过当前路网不存在的边）
            legs: List[Tuple[str, str, float]] = []
            for leg in raw_legs:
                if not isinstance(leg, (list, tuple)) or len(leg) < 3:
                    continue
                eid, from_m, to_m = leg[0], float(leg[1]), float(leg[2])
                if eid not in network.edges:
                    ignored.append(
                        _pair_ignored(
                            trajectory_id,
                            seq_a,
                            seq_b,
                            "edge_not_found",
                            edge_id=eid,
                            detail=f"边 {eid} 不在当前路网中",
                        )
                    )
                    continue
                direction = "forward" if to_m >= from_m else "backward"
                legs.append((eid, direction, abs(to_m - from_m)))

            total_dist = sum(dist for _, _, dist in legs)
            if total_dist <= _MIN_DIST_M:
                ignored.append(
                    _pair_ignored(
                        trajectory_id, seq_a, seq_b, "zero_distance", edge_ids=edge_ids
                    )
                )
                continue

            # 4) 时间按里程占比分摊到各边，得到逐边隐含速度
            for eid, direction, dist in legs:
                if dist <= _MIN_DIST_M:
                    continue
                dur_s = dt_s * dist / total_dist
                speed_kmh = dist / dur_s * 3.6
                if speed_kmh > max_speed_kmh:
                    ignored.append(
                        _pair_ignored(
                            trajectory_id,
                            seq_a,
                            seq_b,
                            "implausible_speed",
                            edge_id=eid,
                            detail=(
                                f"隐含速度 {speed_kmh:.1f}km/h 超过可信上限 "
                                f"{max_speed_kmh:.0f}km/h"
                            ),
                        )
                    )
                    continue
                key = (eid, direction)
                speeds[key].append(speed_kmh)
                sampled_dist[key] += dist
                sampled_dur[key] += dur_s

    edges: List[dict] = []
    for (eid, direction), values in speeds.items():
        edge = network.edges[eid]
        ordered = sorted(values)
        median = _percentile(ordered, 0.5)
        p90 = _percentile(ordered, 0.9)
        edges.append(
            {
                "edge_id": eid,
                "edge_name": edge.name,
                "direction": direction,
                "sample_count": len(values),
                "distance_m": round(edge.length_m, 1),
                "sampled_distance_m": round(sampled_dist[(eid, direction)], 1),
                "sampled_duration_s": round(sampled_dur[(eid, direction)], 1),
                "median_speed_kmh": round(median, 1),
                "p90_speed_kmh": round(p90, 1),
            }
        )
    edges.sort(key=lambda e: (e["edge_id"], e["direction"]))

    ignored.sort(key=lambda r: (r["trajectory_id"], r["seq_from"], r["reason"], r["edge_id"] or ""))
    counts: Dict[str, int] = defaultdict(int)
    for rec in ignored:
        counts[rec["reason"]] += 1

    return {
        "edges": edges,
        "ignored": ignored,
        "ignored_count_by_reason": dict(sorted(counts.items())),
        "total_samples": sum(len(v) for v in speeds.values()),
        "total_ignored": len(ignored),
    }
