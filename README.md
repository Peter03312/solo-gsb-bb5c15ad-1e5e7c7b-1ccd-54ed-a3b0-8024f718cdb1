# GPS 轨迹路网匹配 API

基于 **FastAPI + SQLite** 的可解释 GPS 轨迹地图匹配服务。核心算法为**候选道路投影 + 隐马尔可夫模型（HMM）Viterbi 解码**：对每个定位点按半径筛选候选道路投影，综合 GPS 距离（发射概率）、路网连通性与路径/直线距离差、道路限速与隐含速度（转移概率）解码出全局最优路径；无可行路径时明确返回断段原因。

纯 Python 实现几何与图算法（无 shapely/osmnx 等重型依赖），镜像精简、构建零编译。

## 快速启动

```bash
docker compose up --build
```

服务监听 **8000** 端口，启动时自动加载内置示例路网（`app/data/roads.geojson`，9×9 网格城市，144 条路段）。

- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：`curl http://localhost:8000/api/health`

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
python scripts/generate_roads.py   # 重新生成内置路网与示例轨迹（可选，已提交）
DB_PATH=/tmp/mapmatch.db uvicorn app.main:app --reload --port 8000
pytest tests/                       # 运行测试
```

## 快速体验

提交内置示例轨迹（自动匹配）：

```bash
curl -X POST http://localhost:8000/api/trajectories \
  -H 'Content-Type: application/json' \
  -d @examples/sample_trajectory.json
# => {"trajectory_id": "xxxx", "matched": true, "match_status": "matched", ...}
```

查询匹配结果与导出 GeoJSON：

```bash
TID=<上一步返回的 trajectory_id>
curl http://localhost:8000/api/trajectories/$TID/match
curl http://localhost:8000/api/trajectories/$TID/match/geojson > matched.geojson
# 道路通行画像（多个轨迹 id 用逗号分隔）
curl "http://localhost:8000/api/roads/travel-profile?trajectory_ids=$TID"
```

`matched.geojson` 可直接拖入 [geojson.io](https://geojson.io) 或 QGIS 查看匹配路径。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 + 路网摘要 |
| GET | `/api/roads/summary` | 路网统计（边数、总长、道路名） |
| GET | `/api/roads/travel-profile?trajectory_ids=...` | 道路通行画像：按边与方向聚合典型/高峰速度 |
| POST | `/api/trajectories` | 提交轨迹，默认立即匹配 |
| GET | `/api/trajectories` | 轨迹列表 |
| GET | `/api/trajectories/{id}` | 轨迹详情（含原始点） |
| POST | `/api/trajectories/{id}/match` | （重新）匹配，可覆盖参数 |
| GET | `/api/trajectories/{id}/match` | 匹配结果（逐点明细 + 断段原因） |
| GET | `/api/trajectories/{id}/match/geojson` | 导出 GeoJSON（分段路径 + 投影点 + 断点） |

### 提交轨迹

```json
{
  "name": "早高峰通勤",
  "points": [
    {"lat": 39.9008, "lon": 116.381, "timestamp": "2026-09-21T08:00:00Z", "speed_kmh": 32.5}
  ],
  "match": true,
  "params": {"radius_m": 60, "sigma_m": 20}
}
```

`timestamp` 可选但建议提供：用于速度转移约束。`params` 中所有字段均可选，缺省用环境变量配置。

### 匹配结果（可解释）

`GET /api/trajectories/{id}/match` 返回逐点明细：

- 每点：`edge_id` / `edge_name`（匹配到的路段）、`projected`（投影点）、`distance_m`（GPS 点到投影距离）、`emission_logp`（发射对数概率）、`transition`（转移明细：沿网路径距离、直线距离、距离差、隐含车速、限速、各项对数概率）、`cum_logp`（累计对数概率）
- `breaks`：断段列表，含原因：
  - `no_candidate_within_radius` — 搜索半径内无候选道路
  - `no_feasible_transition` — 路网不连通或隐含速度超过硬上限
- `segments`：匹配成功的连续分段及其沿网几何

### 道路通行画像

`GET /api/roads/travel-profile?trajectory_ids=<id1>,<id2>` 基于**已持久化的匹配结果**，按道路边与行驶方向聚合相邻匹配点的沿网行驶时间/距离：

- 相邻匹配点的 Viterbi 转移明细（`transition.legs`）给出沿网途经的每条边及里程区间；两点间时间按各边里程占比分摊（假设区间内匀速），得到逐边隐含速度
- 同一条边的两个行驶方向分别统计：`forward` 为沿边几何（GeoJSON 坐标顺序）正向，`backward` 为反向
- 速度分布用**中位数**表示典型通行速度（`median_speed_kmh`）、**P90** 表示高峰通行速度（`p90_speed_kmh`，线性插值）

返回示例：

```json
{
  "trajectory_ids": ["ab12cd34ef56"],
  "missing_trajectory_ids": [],
  "unmatched_trajectory_ids": [],
  "matched_count": 1,
  "total_samples": 3,
  "total_ignored": 0,
  "ignored_count_by_reason": {},
  "edges": [
    {
      "edge_id": "wei3-1",
      "edge_name": "纬三路",
      "direction": "forward",
      "sample_count": 3,
      "distance_m": 341.2,
      "sampled_distance_m": 253.4,
      "sampled_duration_s": 27.0,
      "median_speed_kmh": 33.8,
      "p90_speed_kmh": 34.1
    }
  ],
  "ignored": []
}
```

行为说明：

- 轨迹 id 以**逗号分隔**，可传多个；不存在的 id 进入 `missing_trajectory_ids`，存在但尚未匹配的进入 `unmatched_trajectory_ids`，均不导致错误
- 没有任何有效样本（全部 id 无效/未匹配，或样本全部被剔除）时 `edges` 为空数组、HTTP 200，不会返回 500
- 被剔除的样本在 `ignored` 中逐条回显（来源轨迹、相邻点序号、边、原因与说明），并在 `ignored_count_by_reason` 汇总。原因码：
  - `missing_timestamp` — 相邻匹配点至少一端缺少时间戳
  - `non_monotonic_timestamp` — 时间戳逆序或时间差为 0
  - `incoherent_match` — 转移明细缺失/无 legs 或序号不连续（跨断段）
  - `edge_not_found` — 匹配结果引用的边在当前路网中不存在
  - `zero_distance` — 相邻匹配点间沿网距离为 0，无法计算速度
  - `implausible_speed` — 隐含速度超过可信上限（`PROFILE_MAX_SPEED_KMH`，默认 180km/h）

## 算法说明

1. **候选投影**：空间网格索引筛选半径 `MATCH_RADIUS_M` 内的路段，将 GPS 点投影到路段上，得到投影点、垂直距离与沿边里程。
2. **发射概率**：投影距离的高斯分布 `log p = -d²/(2σ²)`，`σ = HMM_SIGMA_M`。
3. **转移概率**（相邻点候选对）：
   - **连通性/路径一致性**：Dijkstra 最短路得到沿网路径距离，与两点直线距离之差按 `log p = -|route-linear|/β` 衰减，`β = HMM_BETA_M`；不连通则概率为 0。
   - **速度约束**：`隐含速度 = 路径距离 / 时间差`，超过 `道路限速 × SPEED_TOLERANCE` 按比例惩罚，超过 `HARD_MAX_SPEED_KMH` 判定不可行（概率 0）。
4. **Viterbi 解码**：在候选图上求累计对数概率最大的路径。某点无候选或所有转移不可行时记录断段原因，并从下一可行点自动重启新段，保证部分匹配结果可用。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ROADS_GEOJSON` | `app/data/roads.geojson` | 路网文件路径（LineString FeatureCollection，路口需已打断；`properties` 支持 `id`/`name`/`maxspeed`） |
| `DB_PATH` | `/data/mapmatch.db` | SQLite 数据库路径 |
| `MATCH_RADIUS_M` | `50` | 候选道路搜索半径（米） |
| `HMM_SIGMA_M` | `15` | GPS 噪声标准差 σ（米） |
| `HMM_BETA_M` | `30` | 路径/直线距离差衰减系数 β（米） |
| `SPEED_TOLERANCE` | `1.4` | 道路限速容忍倍数 |
| `HARD_MAX_SPEED_KMH` | `180` | 隐含速度硬上限（km/h），超过即不可行 |
| `GRID_CELL_M` | `100` | 空间索引网格边长（米） |
| `DEFAULT_ROAD_SPEED_KMH` | `50` | 路网未标注 maxspeed 时的默认限速 |
| `PROFILE_MAX_SPEED_KMH` | `180` | 通行画像中可信速度上限（km/h），超过的样本计入 `implausible_speed` |

使用自己的路网：

```yaml
# docker-compose.yml
volumes:
  - ./my_roads.geojson:/srv/app/app/data/roads.geojson:ro
```

## 项目结构

```
app/
  main.py                 # FastAPI 路由与生命周期
  config.py               # 环境变量配置
  db.py                   # SQLite 持久化
  schemas.py              # 请求/响应模型
  data/roads.geojson      # 内置示例路网（由脚本生成）
  mapmatch/
    geo.py                # 投影与距离计算
    road_network.py       # 路网加载、空间索引、候选投影、Dijkstra
    matcher.py            # HMM + Viterbi 解码、断段处理
    travel_profile.py     # 道路通行画像：沿网时间/距离聚合、中位数/P90、样本剔除
scripts/generate_roads.py # 生成内置路网与示例轨迹
examples/sample_trajectory.json
tests/                    # 单元测试 + API 端到端测试
Dockerfile / docker-compose.yml
```
