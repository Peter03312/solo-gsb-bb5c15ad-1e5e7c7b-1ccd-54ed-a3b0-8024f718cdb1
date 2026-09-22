"""FastAPI 入口：轨迹提交、匹配结果查询、GeoJSON 导出。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from .config import settings
from .db import Database
from .mapmatch.matcher import GpsPoint, MatchParams, match_trajectory
from .mapmatch.road_network import RoadNetwork
from .mapmatch.travel_profile import build_travel_profile
from .schemas import MatchParamsIn, TrajectoryCreated, TrajectorySubmit, TravelProfileResponse

network: Optional[RoadNetwork] = None
db: Optional[Database] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时加载内置路网并初始化数据库。"""
    global network, db
    network = RoadNetwork.from_geojson(
        settings.roads_geojson,
        cell_size_m=settings.grid_cell_m,
        default_speed_kmh=settings.default_road_speed_kmh,
    )
    db = Database(settings.db_path)
    yield


app = FastAPI(
    title="GPS 轨迹路网匹配 API",
    description="基于候选投影 + 隐马尔可夫 Viterbi 解码的可解释地图匹配服务",
    version="1.0.0",
    lifespan=lifespan,
)


def _get_network() -> RoadNetwork:
    assert network is not None
    return network


def _get_db() -> Database:
    assert db is not None
    return db


def _params_from(inp: Optional[MatchParamsIn]) -> MatchParams:
    p = MatchParams(
        radius_m=settings.match_radius_m,
        sigma_m=settings.hmm_sigma_m,
        beta_m=settings.hmm_beta_m,
        speed_tolerance=settings.speed_tolerance,
        hard_max_speed_kmh=settings.hard_max_speed_kmh,
    )
    if inp:
        for k, v in inp.model_dump(exclude_none=True).items():
            setattr(p, k, v)
    return p


def _run_match(tid: str, points: list[dict], params: MatchParams) -> dict:
    gps = []
    for p in points:
        ts = p.get("timestamp")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        gps.append(
            GpsPoint(lat=p["lat"], lon=p["lon"], timestamp=ts, speed_kmh=p.get("speed_kmh"))
        )
    result = match_trajectory(_get_network(), gps, params)
    _get_db().save_match(tid, result["status"], vars(params), result)
    return result


# ---------- 基础 ----------

@app.get("/api/health", tags=["meta"])
def health():
    net = _get_network()
    return {"status": "ok", "network": net.summary()}


@app.get("/api/roads/summary", tags=["meta"])
def roads_summary():
    return _get_network().summary()


@app.get("/api/roads/travel-profile", response_model=TravelProfileResponse, tags=["roads"])
def road_travel_profile(
    trajectory_ids: str = Query(
        ...,
        description="逗号分隔的轨迹 id 列表，如 trajectory_ids=ab12,cd34",
    )
):
    """按道路边与行驶方向聚合已持久化匹配结果的通行画像（中位数/P90 速度）。

    不存在的轨迹与尚未匹配的轨迹不会导致失败，分别在
    missing_trajectory_ids / unmatched_trajectory_ids 中回显；
    没有任何有效样本时 edges 为空。
    """
    # 逗号分隔解析 + 去重（保持请求中的顺序）
    tids: list[str] = []
    seen: set[str] = set()
    for raw in trajectory_ids.split(","):
        tid = raw.strip()
        if tid and tid not in seen:
            seen.add(tid)
            tids.append(tid)
    if not tids:
        raise HTTPException(422, "trajectory_ids 至少需要包含一个轨迹 id")

    database = _get_db()
    existing = database.existing_trajectory_ids(tids)
    missing = [t for t in tids if t not in existing]
    matches = database.get_matches([t for t in tids if t in existing])
    unmatched = [t for t in tids if t in existing and t not in matches]
    used = [t for t in tids if t in matches]

    profile = build_travel_profile(
        _get_network(),
        [(t, matches[t]["result"]) for t in used],
        max_speed_kmh=settings.profile_max_speed_kmh,
    )
    return {
        "trajectory_ids": used,
        "missing_trajectory_ids": missing,
        "unmatched_trajectory_ids": unmatched,
        "matched_count": len(used),
        **profile,
    }


# ---------- 轨迹 ----------

@app.post("/api/trajectories", response_model=TrajectoryCreated, status_code=201, tags=["trajectory"])
def submit_trajectory(body: TrajectorySubmit):
    """提交 GPS 轨迹；默认立即执行路网匹配。"""
    points = [p.model_dump(mode="json") for p in body.points]
    record = _get_db().create_trajectory(body.name, points)
    matched, status = False, None
    if body.match:
        result = _run_match(record["id"], points, _params_from(body.params))
        matched, status = True, result["status"]
    return TrajectoryCreated(
        trajectory_id=record["id"],
        name=record["name"],
        point_count=record["point_count"],
        matched=matched,
        match_status=status,
    )


@app.get("/api/trajectories", tags=["trajectory"])
def list_trajectories():
    return {"items": _get_db().list_trajectories()}


@app.get("/api/trajectories/{tid}", tags=["trajectory"])
def get_trajectory(tid: str):
    traj = _get_db().get_trajectory(tid)
    if traj is None:
        raise HTTPException(404, f"轨迹 {tid} 不存在")
    return traj


# ---------- 匹配 ----------

@app.post("/api/trajectories/{tid}/match", tags=["matching"])
def run_match(tid: str, params: Optional[MatchParamsIn] = None):
    """对已提交的轨迹（重新）执行匹配，可覆盖默认参数。"""
    traj = _get_db().get_trajectory(tid)
    if traj is None:
        raise HTTPException(404, f"轨迹 {tid} 不存在")
    result = _run_match(tid, traj["points"], _params_from(params))
    return {
        "trajectory_id": tid,
        "status": result["status"],
        "matched_count": result["matched_count"],
        "point_count": result["point_count"],
        "breaks": result["breaks"],
    }


@app.get("/api/trajectories/{tid}/match", tags=["matching"])
def get_match(tid: str):
    """查询匹配结果：逐点匹配边、投影位置、发射/转移概率明细与断段原因。"""
    match = _get_db().get_match(tid)
    if match is None:
        raise HTTPException(404, f"轨迹 {tid} 尚未匹配")
    return match


@app.get("/api/trajectories/{tid}/match/geojson", tags=["matching"])
def get_match_geojson(tid: str):
    """导出匹配结果为 GeoJSON：分段匹配路径（LineString）+ 逐点投影（Point）。"""
    match = _get_db().get_match(tid)
    if match is None:
        raise HTTPException(404, f"轨迹 {tid} 尚未匹配")
    result = match["result"]
    features = []
    for i, seg in enumerate(result["segments"]):
        if len(seg["geometry"]) < 2:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": seg["geometry"]},
                "properties": {
                    "kind": "matched_path",
                    "segment_index": i,
                    "start_seq": seg["start_seq"],
                    "end_seq": seg["end_seq"],
                    "edge_names": seg["edge_names"],
                },
            }
        )
    for p in result["points"]:
        if p is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [p["projected"]["lon"], p["projected"]["lat"]],
                },
                "properties": {
                    "kind": "matched_point",
                    "seq": p["seq"],
                    "edge_id": p["edge_id"],
                    "edge_name": p["edge_name"],
                    "distance_m": p["distance_m"],
                    "emission_logp": p["emission_logp"],
                },
            }
        )
    for b in result["breaks"]:
        src = result["points"][b["seq"]] if b["seq"] < len(result["points"]) else None
        obs = src["observed"] if src else None
        features.append(
            {
                "type": "Feature",
                "geometry": (
                    {"type": "Point", "coordinates": [obs["lon"], obs["lat"]]}
                    if obs
                    else None
                ),
                "properties": {"kind": "break", **b},
            }
        )
    return JSONResponse(
        {
            "type": "FeatureCollection",
            "features": features,
            "properties": {
                "trajectory_id": tid,
                "status": result["status"],
                "matched_count": result["matched_count"],
                "point_count": result["point_count"],
            },
        }
    )
