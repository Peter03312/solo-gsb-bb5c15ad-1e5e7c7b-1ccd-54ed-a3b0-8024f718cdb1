"""API 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class GpsPointIn(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="纬度")
    lon: float = Field(..., ge=-180, le=180, description="经度")
    timestamp: Optional[datetime] = Field(None, description="ISO8601 时间，用于速度约束")
    speed_kmh: Optional[float] = Field(None, ge=0, description="设备上报速度（可选，仅记录）")


class MatchParamsIn(BaseModel):
    radius_m: Optional[float] = Field(None, gt=0, le=500, description="候选搜索半径（米）")
    sigma_m: Optional[float] = Field(None, gt=0, description="GPS 噪声标准差（米）")
    beta_m: Optional[float] = Field(None, gt=0, description="路径/直线距离差衰减（米）")
    speed_tolerance: Optional[float] = Field(None, gt=1.0, description="限速容忍倍数")


class TrajectorySubmit(BaseModel):
    name: str = Field("未命名轨迹", max_length=200)
    points: List[GpsPointIn] = Field(..., min_length=2)
    match: bool = Field(True, description="提交后立即执行匹配")
    params: Optional[MatchParamsIn] = None


class TrajectoryCreated(BaseModel):
    trajectory_id: str
    name: str
    point_count: int
    matched: bool
    match_status: Optional[str] = None


class IgnoredSample(BaseModel):
    trajectory_id: str = Field(..., description="来源轨迹 id")
    seq_from: int = Field(..., description="相邻匹配点中前一点的序号")
    seq_to: int = Field(..., description="相邻匹配点中后一点的序号")
    edge_id: Optional[str] = Field(None, description="被忽略边样本所属边（边级问题时存在）")
    edge_ids: List[str] = Field(default_factory=list, description="该相邻对途经的边序列")
    reason: str = Field(..., description="忽略原因码")
    detail: Optional[str] = Field(None, description="人类可读的忽略说明")


class RoadTravelProfileItem(BaseModel):
    edge_id: str
    edge_name: str
    direction: str = Field(..., description="行驶方向：forward 沿边几何正向，backward 反向")
    sample_count: int = Field(..., description="有效边样本数（跨相邻对按里程拆分）")
    distance_m: float = Field(..., description="边几何长度（米）")
    sampled_distance_m: float = Field(..., description="有效样本在该边上的累计行驶里程（米）")
    sampled_duration_s: float = Field(..., description="有效样本在该边上的累计行驶时间（秒）")
    median_speed_kmh: float = Field(..., description="典型通行速度（中位数，km/h）")
    p90_speed_kmh: float = Field(..., description="高峰通行速度（P90，km/h）")


class TravelProfileResponse(BaseModel):
    trajectory_ids: List[str] = Field(default_factory=list, description="实际纳入聚合的轨迹 id")
    missing_trajectory_ids: List[str] = Field(default_factory=list, description="不存在的轨迹 id")
    unmatched_trajectory_ids: List[str] = Field(default_factory=list, description="存在但尚无匹配结果的轨迹 id")
    matched_count: int = Field(0, description="纳入聚合的匹配结果数")
    total_samples: int = Field(0, description="有效边样本总数")
    total_ignored: int = Field(0, description="被忽略样本（相邻对/边级）总数")
    ignored_count_by_reason: dict = Field(
        default_factory=dict, description="按原因码汇总的被忽略样本计数"
    )
    edges: List[RoadTravelProfileItem] = Field(default_factory=list)
    ignored: List[IgnoredSample] = Field(default_factory=list)
