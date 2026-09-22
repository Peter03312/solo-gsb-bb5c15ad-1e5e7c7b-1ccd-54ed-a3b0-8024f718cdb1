"""隐马尔可夫模型地图匹配：候选投影 + Viterbi 解码。

- 发射概率：GPS 点到候选投影点距离的高斯分布  log p = -d²/(2σ²)
- 转移概率：
    * 连通性/路径一致性：沿网最短路径与两点直线距离之差  log p = -|route-linear|/β
    * 速度约束：路径距离/时间差 推出速度，超过道路限速×容忍倍数时施加惩罚，
      超过 HARD_MAX 视为不可行（概率 0）
- 某点无候选或所有转移不可行时记录断段原因，并从下一可行点重新起段。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence

from .geo import haversine_m
from .road_network import Candidate, RoadNetwork

NEG_INF = float("-inf")


@dataclass
class GpsPoint:
    lat: float
    lon: float
    timestamp: Optional[datetime] = None
    speed_kmh: Optional[float] = None  # 设备上报速度（可选，仅作参考记录）


@dataclass
class MatchParams:
    radius_m: float = 50.0
    sigma_m: float = 15.0
    beta_m: float = 30.0
    speed_tolerance: float = 1.4
    hard_max_speed_kmh: float = 180.0


@dataclass
class _State:
    candidate: Candidate
    emission_logp: float


def _emission_logp(dist_m: float, sigma_m: float) -> float:
    return -0.5 * (dist_m / sigma_m) ** 2


def _transition_logp(
    net: RoadNetwork,
    pa: GpsPoint,
    pb: GpsPoint,
    ca: Candidate,
    cb: Candidate,
    params: MatchParams,
) -> tuple[float, dict]:
    """返回 (log 转移概率, 可解释的明细)。NEG_INF 表示不可行。"""
    route_m, legs = net.route_between(ca, cb)
    linear_m = haversine_m(pa.lat, pa.lon, pb.lat, pb.lon)
    detail = {
        "route_distance_m": None if math.isinf(route_m) else round(route_m, 1),
        "linear_distance_m": round(linear_m, 1),
        "distance_diff_m": None,
        "implied_speed_kmh": None,
        "speed_limit_kmh": None,
        "connectivity_logp": None,
        "speed_penalty_logp": 0.0,
        "legs": legs,
    }
    if math.isinf(route_m):
        detail["infeasible_reason"] = "no_route_between_candidates"
        return NEG_INF, detail

    diff = abs(route_m - linear_m)
    conn_logp = -diff / params.beta_m
    detail["distance_diff_m"] = round(diff, 1)
    detail["connectivity_logp"] = round(conn_logp, 4)

    speed_logp = 0.0
    if pa.timestamp is not None and pb.timestamp is not None:
        dt = (pb.timestamp - pa.timestamp).total_seconds()
        if dt > 0:
            speed_kmh = route_m / dt * 3.6
            limit = max(ca.edge.maxspeed_kmh, cb.edge.maxspeed_kmh) * params.speed_tolerance
            detail["implied_speed_kmh"] = round(speed_kmh, 1)
            detail["speed_limit_kmh"] = round(limit, 1)
            if speed_kmh > params.hard_max_speed_kmh:
                detail["infeasible_reason"] = (
                    f"implied_speed {speed_kmh:.0f}km/h > hard max "
                    f"{params.hard_max_speed_kmh:.0f}km/h"
                )
                return NEG_INF, detail
            if speed_kmh > limit:
                # 超速部分按比例惩罚
                speed_logp = -((speed_kmh - limit) / limit) * 5.0
                detail["speed_penalty_logp"] = round(speed_logp, 4)

    return conn_logp + speed_logp, detail


def match_trajectory(
    net: RoadNetwork, points: Sequence[GpsPoint], params: MatchParams
) -> dict:
    """对整条轨迹做 HMM/Viterbi 匹配，返回可解释结果。"""
    # 1. 逐点生成候选
    point_states: List[List[_State]] = []
    breaks: List[dict] = []
    for i, p in enumerate(points):
        cands = net.candidates(p.lon, p.lat, params.radius_m)
        states = [_State(c, _emission_logp(c.dist_m, params.sigma_m)) for c in cands]
        point_states.append(states)
        if not states:
            breaks.append(
                {
                    "seq": i,
                    "reason": "no_candidate_within_radius",
                    "detail": f"半径 {params.radius_m:.0f}m 内没有候选道路",
                }
            )

    # 2. Viterbi 前向递推（含断段重启）
    n = len(points)
    dp: List[List[float]] = [[NEG_INF] * len(s) for s in point_states]
    back: List[List[Optional[int]]] = [[None] * len(s) for s in point_states]
    trans_detail: List[List[Optional[dict]]] = [[None] * len(s) for s in point_states]

    for i in range(n):
        if not point_states[i]:
            continue
        if i == 0 or not dp[i - 1] or all(v == NEG_INF for v in dp[i - 1]):
            # 段首：只考虑发射概率
            for j, st in enumerate(point_states[i]):
                dp[i][j] = st.emission_logp
            continue
        for j, st in enumerate(point_states[i]):
            best_val, best_k, best_detail = NEG_INF, None, None
            for k, prev in enumerate(point_states[i - 1]):
                if dp[i - 1][k] == NEG_INF:
                    continue
                t_logp, detail = _transition_logp(
                    net, points[i - 1], points[i], prev.candidate, st.candidate, params
                )
                if t_logp == NEG_INF:
                    continue
                val = dp[i - 1][k] + t_logp + st.emission_logp
                if val > best_val:
                    best_val, best_k, best_detail = val, k, detail
            dp[i][j] = best_val
            back[i][j] = best_k
            trans_detail[i][j] = best_detail
        if all(v == NEG_INF for v in dp[i]):
            breaks.append(
                {
                    "seq": i,
                    "reason": "no_feasible_transition",
                    "detail": "上一可行点到该点的所有转移均不可行（路网不连通或速度超限），在此断段",
                }
            )

    # 3. 回溯最优路径（可能因断段分为多段，逐段回溯）
    matched: List[Optional[dict]] = [None] * n
    total_logp = 0.0
    matched_any = False
    i = n - 1
    while i >= 0:
        if not dp[i] or max(dp[i]) == NEG_INF:
            i -= 1
            continue
        # 段尾：取该段最优末状态
        j = max(range(len(dp[i])), key=lambda k: dp[i][k])
        total_logp += dp[i][j]
        matched_any = True
        while j is not None and i >= 0:
            st = point_states[i][j]
            cand = st.candidate
            matched[i] = {
                "seq": i,
                "observed": {"lat": points[i].lat, "lon": points[i].lon},
                "timestamp": points[i].timestamp.isoformat() if points[i].timestamp else None,
                **cand.to_dict(),
                "emission_logp": round(st.emission_logp, 4),
                "transition": trans_detail[i][j],
                "cum_logp": round(dp[i][j], 4),
            }
            j = back[i][j]
            i -= 1
    if not matched_any:
        total_logp = NEG_INF

    # 4. 汇总分段与导出几何
    segments = _build_segments(net, matched)
    matched_count = sum(1 for m in matched if m)
    return {
        "status": "matched" if matched_count == n else ("partial" if matched_count else "failed"),
        "point_count": n,
        "matched_count": matched_count,
        "total_logp": None if total_logp == NEG_INF else round(total_logp, 4),
        "params": vars(params),
        "breaks": sorted(breaks, key=lambda b: b["seq"]),
        "segments": segments,
        "points": matched,
    }


def _build_segments(net: RoadNetwork, matched: List[Optional[dict]]) -> List[dict]:
    """把连续匹配点按段组织，并拼接每段的沿网几何。"""
    segments: List[dict] = []
    current: Optional[dict] = None
    for m in matched:
        if m is None:
            current = None
            continue
        if current is None:
            current = {"start_seq": m["seq"], "end_seq": m["seq"], "edges": [], "geometry": []}
            segments.append(current)
        current["end_seq"] = m["seq"]
        eid = m["edge_id"]
        if not current["edges"] or current["edges"][-1]["edge_id"] != eid:
            current["edges"].append({"edge_id": eid, "name": m["edge_name"]})
    # 拼接几何：依次走每个匹配点之间的 legs
    for seg in segments:
        coords = []
        seqs = range(seg["start_seq"], seg["end_seq"] + 1)
        prev = None
        for s in seqs:
            m = matched[s]
            if m is None:
                continue
            if prev is not None and m.get("transition") and m["transition"].get("legs"):
                leg_coords = net.legs_to_geometry(m["transition"]["legs"])
                if coords and leg_coords:
                    coords.extend(leg_coords[1:])
                else:
                    coords.extend(leg_coords)
            else:
                p = m["projected"]
                coords.append((p["lon"], p["lat"]))
            prev = m
        seg["geometry"] = [[round(lon, 7), round(lat, 7)] for lon, lat in coords]
        seg["edge_names"] = [e["name"] for e in seg["edges"]]
        del seg["edges"]
    return segments
