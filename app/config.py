"""应用配置：全部可通过环境变量覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _float(env: str, default: float) -> float:
    return float(os.environ.get(env, default))


def _int(env: str, default: int) -> int:
    return int(os.environ.get(env, default))


@dataclass
class Settings:
    """运行与匹配参数。

    环境变量：
        ROADS_GEOJSON   道路网 GeoJSON 路径（启动时加载）
        DB_PATH         SQLite 数据库文件路径
        MATCH_RADIUS_M  候选道路搜索半径（米）
        HMM_SIGMA_M     发射概率高斯噪声标准差（米）
        HMM_BETA_M      转移概率中路径/直线距离差的衰减系数（米）
        SPEED_TOLERANCE 道路限速容忍倍数
        HARD_MAX_SPEED_KMH 超过即认为转移不可行
        GRID_CELL_M     空间索引网格边长（米）
    """

    roads_geojson: str = field(
        default_factory=lambda: os.environ.get(
            "ROADS_GEOJSON",
            os.path.join(os.path.dirname(__file__), "data", "roads.geojson"),
        )
    )
    db_path: str = field(
        default_factory=lambda: os.environ.get("DB_PATH", "/data/mapmatch.db")
    )
    match_radius_m: float = field(default_factory=lambda: _float("MATCH_RADIUS_M", 50.0))
    hmm_sigma_m: float = field(default_factory=lambda: _float("HMM_SIGMA_M", 15.0))
    hmm_beta_m: float = field(default_factory=lambda: _float("HMM_BETA_M", 30.0))
    speed_tolerance: float = field(default_factory=lambda: _float("SPEED_TOLERANCE", 1.4))
    hard_max_speed_kmh: float = field(
        default_factory=lambda: _float("HARD_MAX_SPEED_KMH", 180.0)
    )
    grid_cell_m: float = field(default_factory=lambda: _float("GRID_CELL_M", 100.0))
    default_road_speed_kmh: float = field(
        default_factory=lambda: _float("DEFAULT_ROAD_SPEED_KMH", 50.0)
    )
    profile_max_speed_kmh: float = field(
        default_factory=lambda: _float("PROFILE_MAX_SPEED_KMH", 180.0)
    )


settings = Settings()
