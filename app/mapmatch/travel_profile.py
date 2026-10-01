"""道路通行画像：基于已持久化的匹配结果，按道路边及行驶方向聚合通行速度。

输入为 ``matcher.match_trajectory`` 产出并持久化的结果。对每条轨迹中相邻的两个
成功匹配点：

1. 校验样本：时间戳缺失 / 逆序 / 不增、断段两侧不连贯、沿网距离为 0 的样本直接剔除，
   并记录可解释的忽略原因；
2. 取转移明细中的沿网 ``legs``（途经边序列及各边 [from_m, to_m] 里程区间）；
3. 假设该转移区间内匀速，按各边里程占比分摊时间差，得到 (边, 方向) 维度的
   距离 / 时长 / 速度样本；
4. 按 (edge_id, direction) 汇总样本数、总里程，并用中位数（典型速度）与 P90
   （高峰速度）刻画该边该方向的通行水平。

纯标准库实现；P90 采用线性插值定义（与 numpy.percentile 默认行为一致）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .road_network import RoadNetwork

# 行驶方向：以边几何坐标（首节点 -> 末节点）为正向
DIRECTION_FORWARD = "forward"
DIRECTION_BACKWARD = "backward"

# 被忽略样本原因
REASON_MISSING_TIMESTAMP = "missing_timestamp"          # 相邻点之一缺时间戳
REASON_TIMESTAMP_REVERSED = "timestamp_reversed"        # 后点时间戳早于前点
REASON_TIMESTAMP_NOT_INCREASING = "timestamp_not_increasing"  # 相邻点时间戳相同
REASON_NOT_CONTIGUOUS = "not_contiguous"                # 断段两侧 / 无沿网路径
REASON_ZERO_DISTANCE = "zero_route_distance"            # 沿网距离为 0，无法测速


@dataclass
class _Bin:
    """一条边一个方向上的样本累计。"""

    speeds_kmh: List[float] = field(default_factory=list)
    distance_m: float = 0.0
    duration_s: float = 0.0

    def add(self, distance_m: float, duration_s: float) -> None:
        self.distance_m += distance_m
        self.duration_s += duration_s
        self.speeds_kmh.append(distance_m / duration_s * 3.6)


def _parse_timestamp(value: object) -> Optional[datetime]:
    """解析匹配结果中的 ISO8601 时间戳；naive 时间一律按 UTC 处理。"""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def percentile(values: Sequence[float], q: float) -> float:
    """线性插值分位数（q ∈ [0,1]，同 numpy.percentile 的默认定义）。"""
    if not values:
        raise ValueError("空样本无法计算分位数")
    vals = sorted(values)
    if len(vals) == 1:
        return vals[0]
    pos = q * (len(vals) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def _extract_leg_samples(
    prev: dict, cur: dict
) -> Tuple[Optional[List[Tuple[str, str, float, float]]], Optional[str], Optional[str]]:
    """把一对相邻匹配点拆成 (edge_id, direction, distance_m, duration_s) 样本。

    返回 (samples, reason, detail)；reason 非空表示该对样本被剔除。
    """
    seq = cur.get("seq")
    prev_seq = prev.get("seq")
    t0 = _parse_timestamp(prev.get("timestamp"))
    t1 = _parse_timestamp(cur.get("timestamp"))
    if t0 is None or t1 is None:
        return (
            None,
            REASON_MISSING_TIMESTAMP,
            f"相邻匹配点（seq {prev_seq} -> {seq}）缺少时间戳，无法计算通行时长",
        )
    dt_s = (t1 - t0).total_seconds()
    if dt_s < 0:
        return (
            None,
            REASON_TIMESTAMP_REVERSED,
            f"时间戳逆序：{prev.get('timestamp')} -> {cur.get('timestamp')}",
        )
    if dt_s == 0:
        return (
            None,
            REASON_TIMESTAMP_NOT_INCREASING,
            f"相邻匹配点（seq {prev_seq} -> {seq}）时间戳相同，时间差为 0",
        )

    legs = (cur.get("transition") or {}).get("legs")
    if not legs:
        return (
            None,
            REASON_NOT_CONTIGUOUS,
            f"相邻匹配点（seq {prev_seq} -> {seq}）之间无沿网转移路径（断段两侧或为段首点）",
        )

    spans: List[Tuple[str, bool, float]] = []
    for leg in legs:
        edge_id, from_m, to_m = leg
        leg_dist = abs(float(to_m) - float(from_m))
        if leg_dist > 0:  # 投影恰好落在节点上时会产生 0 长度腿，忽略即可
            spans.append((str(edge_id), float(to_m) > float(from_m), leg_dist))
    total_m = sum(d for _, _, d in spans)
    if total_m <= 0:
        return (
            None,
            REASON_ZERO_DISTANCE,
            f"相邻匹配点（seq {prev_seq} -> {seq}）沿网距离为 0，无法判定速度与方向",
        )

    samples = [
        (
            edge_id,
            DIRECTION_FORWARD if forward else DIRECTION_BACKWARD,
            dist_m,
            dt_s * dist_m / total_m,
        )
        for edge_id, forward, dist_m in spans
    ]
    return samples, None, None


def build_travel_profile(
    trajectory_results: Iterable[Tuple[str, dict]],
    network: Optional["RoadNetwork"] = None,
) -> dict:
    """汇总多条已匹配轨迹的道路通行画像。

    参数:
        trajectory_results: [(trajectory_id, match_result), ...]，match_result 为
            持久化匹配结果中的 ``result`` 对象。
        network: 可选路网，用于补全边名称与边长。
    """
    bins: Dict[Tuple[str, str], _Bin] = {}
    ignored_samples: List[dict] = []
    transition_count = 0
    trajectory_count = 0

    for trajectory_id, result in trajectory_results:
        trajectory_count += 1
        prev: Optional[dict] = None
        for cur in (result or {}).get("points") or []:
            if cur is None:
                # 未匹配点：匹配在此断段，前后样本不连贯
                prev = None
                continue
            if prev is not None:
                transition_count += 1
                samples, reason, detail = _extract_leg_samples(prev, cur)
                if reason is not None:
                    ignored_samples.append(
                        {
                            "trajectory_id": trajectory_id,
                            "seq": cur.get("seq"),
                            "reason": reason,
                            "detail": detail,
                        }
                    )
                else:
                    for edge_id, direction, dist_m, dur_s in samples:
                        bins.setdefault((edge_id, direction), _Bin()).add(dist_m, dur_s)
            prev = cur

    edges: List[dict] = []
    for edge_id, direction in sorted(
        bins, key=lambda k: (k[0], 0 if k[1] == DIRECTION_FORWARD else 1)
    ):
        b = bins[(edge_id, direction)]
        edge = network.edges.get(edge_id) if network is not None else None
        edges.append(
            {
                "edge_id": edge_id,
                "edge_name": edge.name if edge is not None else None,
                "direction": direction,
                "sample_count": len(b.speeds_kmh),
                "edge_length_m": round(edge.length_m, 1) if edge is not None else None,
                "distance_m": round(b.distance_m, 1),
                "duration_s": round(b.duration_s, 1),
                "median_speed_kmh": round(percentile(b.speeds_kmh, 0.5), 1),
                "p90_speed_kmh": round(percentile(b.speeds_kmh, 0.9), 1),
            }
        )

    return {
        "trajectory_count": trajectory_count,
        "transition_count": transition_count,
        "ignored_sample_count": len(ignored_samples),
        "ignored_trajectories": [],
        "ignored_samples": ignored_samples,
        "edges": edges,
    }
