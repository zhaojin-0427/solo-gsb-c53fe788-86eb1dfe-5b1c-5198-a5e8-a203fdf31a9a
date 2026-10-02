# 双层电路板网格布线服务（two-layer-grid-router）

基于 **Python + FastAPI + OR-Tools (CP-SAT)** 的双层电路板网格布线服务。对所有网络
**联合求解**，先最小化总过孔数，再最小化同层移动总步数，通过 `POST /route` 对外提供
HTTP API，支持 Docker Compose 一键启动。

## 问题规则

- 网格宽、高均为 2～20 的整数，坐标 0 起始：`0 <= x < width`，`0 <= y < height`。
- 两层（`layer` 取 `0` 或 `1`），每层可有各自的禁布格。
- 1～6 条双焊盘网络；焊盘标明层和坐标；每条网有非负整数过孔上限。
- 走线仅沿**同层正交相邻格**移动；过孔在原坐标占两层（层间切换只能经过过孔）。
- 每条路径不得重复占格；**异网**在同层占格不得重合，也不得正交相邻（焊盘同样占格）。
- 目标（字典序）：先最小化总过孔数，再最小化同层移动总步数。
- 求解时限 10 秒：只有完成证明才报告 `optimal` / `infeasible`；超时则区分
  `feasible`（已有可行方案，未证最优）与 `unknown`（尚未找到任何方案）。

## 快速开始

### Docker Compose（一键启动）

```bash
docker compose up --build
```

服务监听 <http://localhost:8000>，交互式 API 文档（Swagger UI）见
<http://localhost:8000/docs>。

### 本地运行（开发）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # 运行依赖
.venv/bin/pip install -r requirements-dev.txt    # 测试依赖（可选）
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q
```

## API 说明

### `POST /route`

请求体：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `grid.width` / `grid.height` | int | 2～20 |
| `blocked.layer0` / `blocked.layer1` | `[[x, y], ...]` | 可省略；格子须在界内 |
| `nets` | array | 1～6 条 |
| `nets[i].name` | string | 可选，响应中原样返回（缺省为 `N1`、`N2`…） |
| `nets[i].pads` | array | 恰好 2 个焊盘，每个 `{layer, x, y}`，`layer ∈ {0, 1}` |
| `nets[i].via_limit` | int | ≥ 0，该网允许使用的过孔数上限 |

响应体（HTTP 200）：

| 字段 | 含义 |
| --- | --- |
| `status` | `optimal`：已证最优；`feasible`：时限内找到可行解但未证最优；`infeasible`：已证无解；`unknown`：时限内尚未找到任何方案 |
| `proven` | 仅 `optimal` / `infeasible` 时为 `true` |
| `solve_time_seconds` / `time_limit_seconds` | 实际求解耗时 / 求解时限（10s） |
| `objectives` | `{"total_vias": …, "total_moves": …}`；`infeasible`/`unknown` 时为 `null` |
| `nets[i].path` | 该网从 `pads[0]` 到 `pads[1]` 的有序格子序列 `[{layer, x, y}, …]` |
| `nets[i].vias` / `via_count` / `move_count` | 过孔坐标 `[x, y]` 列表 / 过孔数 / 同层移动步数 |

以下非法请求返回 **HTTP 422**：网格尺寸越界、焊盘越界、焊盘落在本层禁布格、
任意焊盘重复（同层同坐标，含同一网内两焊盘相同）、过孔上限为负数、网络数量
不在 1～6、焊盘数不为 2、禁布格越界等。

其余端点：`GET /`（服务信息）、`GET /health`（健康检查）。

## 实际请求与响应示例

### 1. 可证明最优（`optimal`）：5×5 双网交叉，B 网经第 2 层跨越 A 网

```bash
curl -s -X POST http://localhost:8000/route \
  -H "Content-Type: application/json" \
  -d '{
    "grid": {"width": 5, "height": 5},
    "blocked": {"layer0": [], "layer1": []},
    "nets": [
      {"name": "A", "via_limit": 2,
       "pads": [{"layer": 0, "x": 0, "y": 2}, {"layer": 0, "x": 4, "y": 2}]},
      {"name": "B", "via_limit": 2,
       "pads": [{"layer": 0, "x": 2, "y": 0}, {"layer": 0, "x": 2, "y": 4}]}
    ]
  }'
```

实际响应（A 网沿 0 层直行；B 网在 `(2,0)`、`(2,4)` 各打 1 个过孔，从 1 层跨过 A 网；
总过孔 2、总步数 8，已证最优）：

```json
{
  "status": "optimal",
  "proven": true,
  "solve_time_seconds": 0.080395,
  "time_limit_seconds": 10.0,
  "detail": "proven optimal: total vias minimized first, then total moves",
  "objectives": {"total_vias": 2, "total_moves": 8},
  "nets": [
    {
      "name": "A",
      "path": [
        {"layer": 0, "x": 0, "y": 2},
        {"layer": 0, "x": 1, "y": 2},
        {"layer": 0, "x": 2, "y": 2},
        {"layer": 0, "x": 3, "y": 2},
        {"layer": 0, "x": 4, "y": 2}
      ],
      "vias": [],
      "via_count": 0,
      "move_count": 4
    },
    {
      "name": "B",
      "path": [
        {"layer": 0, "x": 2, "y": 0},
        {"layer": 1, "x": 2, "y": 0},
        {"layer": 1, "x": 2, "y": 1},
        {"layer": 1, "x": 2, "y": 2},
        {"layer": 1, "x": 2, "y": 3},
        {"layer": 1, "x": 2, "y": 4},
        {"layer": 0, "x": 2, "y": 4}
      ],
      "vias": [[2, 0], [2, 4]],
      "via_count": 2,
      "move_count": 4
    }
  ]
}
```

### 2. 已证无解（`infeasible`）：过孔上限为 0 但两焊盘分属不同层

```bash
curl -s -X POST http://localhost:8000/route \
  -H "Content-Type: application/json" \
  -d '{
    "grid": {"width": 3, "height": 3},
    "nets": [
      {"via_limit": 0,
       "pads": [{"layer": 0, "x": 0, "y": 0}, {"layer": 1, "x": 2, "y": 2}]}
    ]
  }'
```

实际响应：

```json
{
  "status": "infeasible",
  "proven": true,
  "solve_time_seconds": 0.00274,
  "time_limit_seconds": 10.0,
  "detail": "proven infeasible: no routing satisfies all constraints",
  "objectives": null,
  "nets": null
}
```

### 3. 超时已有可行方案（`feasible`）：20×20、6 条网

请求（保存为 `big.json` 后 `curl -s -X POST ... -d @big.json`）：

```json
{
  "grid": {"width": 20, "height": 20},
  "blocked": {"layer0": [[5, 5], [5, 6], [6, 5]], "layer1": [[10, 10]]},
  "nets": [
    {"name": "N1", "via_limit": 4, "pads": [{"layer": 0, "x": 2, "y": 0},  {"layer": 1, "x": 8, "y": 10}]},
    {"name": "N2", "via_limit": 4, "pads": [{"layer": 1, "x": 0, "y": 14}, {"layer": 0, "x": 3, "y": 14}]},
    {"name": "N3", "via_limit": 4, "pads": [{"layer": 0, "x": 16, "y": 11},{"layer": 1, "x": 15, "y": 7}]},
    {"name": "N4", "via_limit": 4, "pads": [{"layer": 1, "x": 7, "y": 12}, {"layer": 0, "x": 12, "y": 9}]},
    {"name": "N5", "via_limit": 4, "pads": [{"layer": 0, "x": 5, "y": 3},  {"layer": 1, "x": 4, "y": 15}]},
    {"name": "N6", "via_limit": 4, "pads": [{"layer": 1, "x": 6, "y": 5},  {"layer": 0, "x": 19, "y": 7}]}
  ]
}
```

实际响应（求解满 10 秒，已找到可行方案但未证最优；各网路径较长，此处截断，
`status`/`objectives`/各网计数为真实返回值）：

```json
{
  "status": "feasible",
  "proven": false,
  "solve_time_seconds": 10.402032,
  "time_limit_seconds": 10.0,
  "detail": "best solution found within the 10s time limit; optimality not proven",
  "objectives": {"total_vias": 6, "total_moves": 62},
  "nets": [
    {"name": "N1", "path": [{"layer": 0, "x": 2, "y": 0}, "..."], "vias": [[2, 0]],  "via_count": 1, "move_count": 16},
    {"name": "N2", "path": [{"layer": 1, "x": 0, "y": 14}, "..."], "vias": [[3, 14]], "via_count": 1, "move_count": 3},
    {"name": "N3", "path": ["..."], "vias": [[16, 10]], "via_count": 1, "move_count": 5},
    {"name": "N4", "path": ["..."], "vias": [[12, 9]],  "via_count": 1, "move_count": 8},
    {"name": "N5", "path": ["..."], "vias": [[5, 3]],   "via_count": 1, "move_count": 15},
    {"name": "N6", "path": ["..."], "vias": [[6, 5]],   "via_count": 1, "move_count": 15}
  ]
}
```

> 若 10 秒内连一个可行方案都未找到，则返回 `"status": "unknown"`，
> `objectives` 与 `nets` 均为 `null`。

### 4. 非法请求（HTTP 422）：焊盘落在禁布格

```bash
curl -s -X POST http://localhost:8000/route \
  -H "Content-Type: application/json" \
  -d '{
    "grid": {"width": 4, "height": 4},
    "blocked": {"layer0": [[1, 1]], "layer1": []},
    "nets": [
      {"via_limit": 1,
       "pads": [{"layer": 0, "x": 1, "y": 1}, {"layer": 0, "x": 3, "y": 3}]}
    ]
  }'
```

实际响应（HTTP 422）：

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body"],
      "msg": "Value error, net #0: pad (1, 1) lies on a blocked cell of layer 0",
      "input": {"grid": {"width": 4, "height": 4}, "...": "..."}
    }
  ]
}
```

## 求解模型简介（`app/solver.py`）

- 每条网、每个可用格子一个占用布尔量；每条无向边（同层移动边 + 过孔边）两个
  方向各一个有向弧布尔量。焊盘 `pads[0]` 出度 1/入度 0，`pads[1]` 入度 1/出度 0，
  其余被占格出入度均为 1；MTZ 位置约束（沿已用弧严格递增）消除孤立环，保证
  每条路径简单不重复占格。
- 过孔是连接同坐标两层的边，使用时同时占用两层；每网过孔数不超过其上限。
- 异网约束：同层任一格至多被一网占用；同层正交相邻格不得分属两网。
- 目标为单一加权和：`总过孔数 × (格点数+1) + 总移动步数`，权重严格大于任何
  可达步数，因此等价于"先过孔后步数"的字典序优化。
- 求解前先用一个快速顺序布线器（逐网 Dijkstra，状态含已用过孔数，避开已占用
  及其同层邻接格）构造可行解，作为 CP-SAT 的搜索提示（初始 incumbent）。
- 时限固定 10 秒（`app/solver.py` 中 `TIME_LIMIT_SECONDS`）。

## 项目结构

```
app/
  main.py      # FastAPI 入口：POST /route、GET /、GET /health
  schemas.py   # 请求/响应模型与 422 校验
  solver.py    # CP-SAT 联合求解 + 顺序布线提示
tests/
  test_api.py  # 22 个端到端测试（含解校验器与 422 用例）
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
