"""道路网络：GeoJSON 加载、空间索引、候选投影、最短路（Dijkstra）。

约定：
- GeoJSON 为 LineString FeatureCollection，路口处必须已经打断（结点共享坐标），
  内置数据 scripts/generate_roads.py 生成的路网满足该条件。
- 所有道路按双向处理。
"""
from __future__ import annotations

import heapq
import json
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .geo import (
    LocalProjector,
    Point,
    haversine_m,
    project_point_to_segment,
)

_NODE_PRECISION = 6  # ~0.1m，用于端点吸附


def _node_key(lon: float, lat: float) -> Tuple[float, float]:
    return (round(lon, _NODE_PRECISION), round(lat, _NODE_PRECISION))


@dataclass
class Edge:
    id: str
    name: str
    coords: List[Point]  # [(lon, lat), ...]
    maxspeed_kmh: float
    length_m: float
    cumlen_m: List[float]  # 每个顶点的累计里程
    start_node: Tuple[float, float]
    end_node: Tuple[float, float]

    def position_at(self, dist_m: float, projector: LocalProjector) -> Point:
        """边上距起点 dist_m 处的经纬度（线性插值）。"""
        d = max(0.0, min(self.length_m, dist_m))
        for i in range(1, len(self.cumlen_m)):
            if self.cumlen_m[i] >= d:
                seg = self.cumlen_m[i] - self.cumlen_m[i - 1]
                t = 0.0 if seg == 0 else (d - self.cumlen_m[i - 1]) / seg
                lon = self.coords[i - 1][0] + t * (self.coords[i][0] - self.coords[i - 1][0])
                lat = self.coords[i - 1][1] + t * (self.coords[i][1] - self.coords[i - 1][1])
                return (lon, lat)
        return self.coords[-1]

    def slice(self, from_m: float, to_m: float, projector: LocalProjector) -> List[Point]:
        """截取边上 [from_m, to_m] 区间的几何（含插值端点）。"""
        lo, hi = sorted((max(0.0, from_m), min(self.length_m, to_m)))
        pts = [self.position_at(lo, projector)]
        for i, c in enumerate(self.cumlen_m):
            if lo < c < hi:
                pts.append(self.coords[i])
        pts.append(self.position_at(hi, projector))
        return pts


@dataclass
class Candidate:
    """一个 GPS 点在一条边上的投影候选。"""

    edge: Edge
    dist_m: float          # GPS 点到投影点的垂直距离
    along_m: float         # 投影点沿边的里程
    proj_lon: float
    proj_lat: float

    def to_dict(self) -> dict:
        return {
            "edge_id": self.edge.id,
            "edge_name": self.edge.name,
            "distance_m": round(self.dist_m, 2),
            "along_m": round(self.along_m, 2),
            "projected": {"lon": self.proj_lon, "lat": self.proj_lat},
        }


class RoadNetwork:
    def __init__(self, cell_size_m: float = 100.0, default_speed_kmh: float = 50.0):
        self.edges: Dict[str, Edge] = {}
        self.adjacency: Dict[Tuple[float, float], List[Tuple[Tuple[float, float], str, float]]] = {}
        self._grid: Dict[Tuple[int, int], List[str]] = {}
        self._cell = cell_size_m
        self._default_speed = default_speed_kmh
        self.projector: Optional[LocalProjector] = None
        self._dijkstra_cache: Dict[Tuple[float, float], Dict[Tuple[float, float], Tuple[float, list]]] = {}

    # ---------- 加载 ----------

    @classmethod
    def from_geojson(
        cls, path: str, cell_size_m: float = 100.0, default_speed_kmh: float = 50.0
    ) -> "RoadNetwork":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        net = cls(cell_size_m=cell_size_m, default_speed_kmh=default_speed_kmh)
        features = data.get("features", [])
        if not features:
            raise ValueError(f"路网文件 {path} 中没有 feature")
        ref_lat = sum(
            c[1]
            for ft in features
            for c in ft["geometry"]["coordinates"]
        ) / max(1, sum(len(ft["geometry"]["coordinates"]) for ft in features))
        net.projector = LocalProjector(ref_lat)
        for i, ft in enumerate(features):
            geom = ft.get("geometry") or {}
            if geom.get("type") != "LineString":
                continue
            props = ft.get("properties") or {}
            coords = [(float(lon), float(lat)) for lon, lat, *_ in geom["coordinates"]]
            if len(coords) < 2:
                continue
            net._add_edge(
                edge_id=str(props.get("id", f"edge-{i}")),
                name=str(props.get("name", f"未命名道路-{i}")),
                coords=coords,
                maxspeed_kmh=float(props.get("maxspeed", default_speed_kmh)),
            )
        if not net.edges:
            raise ValueError(f"路网文件 {path} 中没有可用的 LineString")
        return net

    def _add_edge(self, edge_id: str, name: str, coords: List[Point], maxspeed_kmh: float) -> None:
        cumlen = [0.0]
        for a, b in zip(coords, coords[1:]):
            cumlen.append(cumlen[-1] + haversine_m(a[1], a[0], b[1], b[0]))
        edge = Edge(
            id=edge_id,
            name=name,
            coords=coords,
            maxspeed_kmh=maxspeed_kmh,
            length_m=cumlen[-1],
            cumlen_m=cumlen,
            start_node=_node_key(*coords[0]),
            end_node=_node_key(*coords[-1]),
        )
        self.edges[edge_id] = edge
        # 图邻接（双向）
        for u, v in ((edge.start_node, edge.end_node), (edge.end_node, edge.start_node)):
            self.adjacency.setdefault(u, []).append((v, edge_id, edge.length_m))
        # 网格空间索引：把边的外包框覆盖到的格子都登记
        assert self.projector is not None
        xs, ys = zip(*(self.projector.to_xy(lon, lat) for lon, lat in coords))
        x0, x1 = min(xs) - self._cell, max(xs) + self._cell
        y0, y1 = min(ys) - self._cell, max(ys) + self._cell
        for gx in range(math.floor(x0 / self._cell), math.floor(x1 / self._cell) + 1):
            for gy in range(math.floor(y0 / self._cell), math.floor(y1 / self._cell) + 1):
                self._grid.setdefault((gx, gy), []).append(edge_id)

    # ---------- 候选搜索 ----------

    def candidates(self, lon: float, lat: float, radius_m: float) -> List[Candidate]:
        """找出半径内所有边的投影候选，按距离升序。"""
        assert self.projector is not None
        px, py = self.projector.to_xy(lon, lat)
        gx, gy = math.floor(px / self._cell), math.floor(py / self._cell)
        reach = math.ceil(radius_m / self._cell) + 1
        edge_ids: List[str] = []
        seen = set()
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                for eid in self._grid.get((gx + dx, gy + dy), ()):
                    if eid not in seen:
                        seen.add(eid)
                        edge_ids.append(eid)
        out: List[Candidate] = []
        for eid in edge_ids:
            edge = self.edges[eid]
            best: Optional[Candidate] = None
            along = 0.0
            for a, b in zip(edge.coords, edge.coords[1:]):
                ax, ay = self.projector.to_xy(*a)
                bx, by = self.projector.to_xy(*b)
                qx, qy, t, dist = project_point_to_segment(px, py, ax, ay, bx, by)
                if dist <= radius_m and (best is None or dist < best.dist_m):
                    seg_len = haversine_m(a[1], a[0], b[1], b[0])
                    qlon, qlat = self.projector.to_lonlat(qx, qy)
                    best = Candidate(
                        edge=edge,
                        dist_m=dist,
                        along_m=along + t * seg_len,
                        proj_lon=qlon,
                        proj_lat=qlat,
                    )
                along += haversine_m(a[1], a[0], b[1], b[0])
            if best is not None:
                out.append(best)
        out.sort(key=lambda c: c.dist_m)
        return out

    # ---------- 路由 ----------

    def _dijkstra(self, source: Tuple[float, float]) -> Dict[Tuple[float, float], Tuple[float, list]]:
        """单源最短路，返回 {node: (dist, [(prev_node, edge_id), ...])}，带缓存。"""
        if source in self._dijkstra_cache:
            return self._dijkstra_cache[source]
        dist = {source: 0.0}
        prev: Dict[Tuple[float, float], Tuple[Tuple[float, float], str]] = {}
        pq = [(0.0, source)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, math.inf):
                continue
            for v, eid, w in self.adjacency.get(u, ()):
                nd = d + w
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    prev[v] = (u, eid)
                    heapq.heappush(pq, (nd, v))
        result = {n: (dist[n], self._reconstruct(prev, source, n)) for n in dist}
        self._dijkstra_cache[source] = result
        return result

    @staticmethod
    def _reconstruct(prev, source, target):
        path = []
        node = target
        while node != source:
            pnode, eid = prev[node]
            path.append((pnode, eid))
            node = pnode
        path.reverse()
        return path

    def route_between(
        self, a: Candidate, b: Candidate
    ) -> Tuple[float, List[Tuple[str, float, float]]]:
        """两候选点间的最短沿网距离与途经边序列。

        返回 (distance_m, [(edge_id, from_m, to_m), ...])；
        不可达时返回 (inf, [])。同边时允许直接沿边行驶（双向假设）。
        """
        if a.edge.id == b.edge.id:
            d = abs(b.along_m - a.along_m)
            return d, [(a.edge.id, a.along_m, b.along_m)]

        best = (math.inf, [])
        # 从 a 的某个端点离开，自 b 的某个端点进入
        for exit_node, a_leg in (
            (a.edge.end_node, a.edge.length_m - a.along_m),
            (a.edge.start_node, a.along_m),
        ):
            sp = self._dijkstra(exit_node)
            for entry_node, b_leg in (
                (b.edge.start_node, b.along_m),
                (b.edge.end_node, b.edge.length_m - b.along_m),
            ):
                if entry_node not in sp:
                    continue
                mid_dist, mid_path = sp[entry_node]
                total = a_leg + mid_dist + b_leg
                if total < best[0]:
                    legs: List[Tuple[str, float, float]] = []
                    legs.append(
                        (a.edge.id, a.along_m, a.along_m + a_leg)
                        if exit_node == a.edge.end_node
                        else (a.edge.id, a.along_m, a.along_m - a_leg)
                    )
                    for from_node, eid in mid_path:
                        e = self.edges[eid]
                        if e.start_node == from_node:
                            legs.append((eid, 0.0, e.length_m))
                        else:
                            legs.append((eid, e.length_m, 0.0))
                    legs.append(
                        (b.edge.id, 0.0, b.along_m)
                        if entry_node == b.edge.start_node
                        else (b.edge.id, b.edge.length_m, b.along_m)
                    )
                    best = (total, legs)
        return best

    def legs_to_geometry(self, legs: Sequence[Tuple[str, float, float]]) -> List[Point]:
        """把途经边序列拼成一条折线几何。"""
        assert self.projector is not None
        coords: List[Point] = []
        for eid, frm, to in legs:
            edge = self.edges[eid]
            seg = edge.slice(frm, to, self.projector)
            if frm > to:
                seg = list(reversed(seg))
            if coords and seg and _node_key(*coords[-1]) == _node_key(*seg[0]):
                coords.extend(seg[1:])
            else:
                coords.extend(seg)
        return coords

    def summary(self) -> dict:
        return {
            "edge_count": len(self.edges),
            "node_count": len(self.adjacency),
            "total_length_km": round(sum(e.length_m for e in self.edges.values()) / 1000, 2),
            "roads": sorted({e.name for e in self.edges.values()}),
        }
