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
