"""SQLite 持久化层（标准库 sqlite3，线程安全按请求开连接）。"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional, Sequence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trajectories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    point_count INTEGER NOT NULL,
    points_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS matches (
    trajectory_id TEXT PRIMARY KEY REFERENCES trajectories(id),
    status TEXT NOT NULL,
    params_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ---------- 轨迹 ----------

    def create_trajectory(self, name: str, points: list[dict]) -> dict:
        tid = uuid.uuid4().hex[:12]
        created = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO trajectories VALUES (?,?,?,?,?)",
                (tid, name, created, len(points), json.dumps(points, ensure_ascii=False)),
            )
        return {"id": tid, "name": name, "created_at": created, "point_count": len(points)}

    def get_trajectory(self, tid: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM trajectories WHERE id=?", (tid,)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "point_count": row["point_count"],
            "points": json.loads(row["points_json"]),
        }

    def list_trajectories(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, created_at, point_count FROM trajectories ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def existing_trajectory_ids(self, tids: Sequence[str]) -> set[str]:
        """返回给定 id 中实际存在的轨迹 id 集合。"""
        if not tids:
            return set()
        placeholders = ",".join("?" * len(tids))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id FROM trajectories WHERE id IN ({placeholders})", tuple(tids)
            ).fetchall()
        return {r["id"] for r in rows}

    # ---------- 匹配结果 ----------

    def save_match(self, tid: str, status: str, params: dict, result: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO matches VALUES (?,?,?,?,?)",
                (tid, status, json.dumps(params), json.dumps(result, ensure_ascii=False), _now()),
            )

    def get_match(self, tid: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM matches WHERE trajectory_id=?", (tid,)).fetchone()
        if row is None:
            return None
        return {
            "trajectory_id": row["trajectory_id"],
            "status": row["status"],
            "params": json.loads(row["params_json"]),
            "result": json.loads(row["result_json"]),
            "created_at": row["created_at"],
        }

    def get_matches(self, tids: Sequence[str]) -> dict[str, dict]:
        """批量读取匹配结果，返回 {trajectory_id: match}（跳过未匹配的轨迹）。"""
        if not tids:
            return {}
        placeholders = ",".join("?" * len(tids))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM matches WHERE trajectory_id IN ({placeholders})", tuple(tids)
            ).fetchall()
        return {
            row["trajectory_id"]: {
                "trajectory_id": row["trajectory_id"],
                "status": row["status"],
                "params": json.loads(row["params_json"]),
                "result": json.loads(row["result_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        }
