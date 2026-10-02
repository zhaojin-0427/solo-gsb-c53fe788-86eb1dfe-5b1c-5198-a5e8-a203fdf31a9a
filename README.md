# 双层电路板网格布线服务

基于 **Python + FastAPI + OR-Tools (CP-SAT)** 的双层电路板网格布线服务。
对所有网络**联合求解**，先最小化总过孔数，再最小化同层移动总步数，
并只在证明完成后才报告 `optimal` / `infeasible`。

## 快速开始

### Docker Compose（一键启动）

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`：

- `POST /route` — 布线接口
- `GET  /health` — 健康检查
- `GET  /docs` — Swagger UI 交互文档

### 本地开发

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install pytest httpx
python -m pytest tests/ -q
```

## 布线规则

- 网格宽、高均为 2～20 的整数；层编号为 `0` / `1`。
- 请求含两层各自的禁布格（`blocked`）以及 1～6 条双焊盘网络（`nets`）。
- 走线只能沿**同层正交相邻格**移动；**过孔**在原坐标打通两层（占两层各一格）。
- 每条网络的路径不得重复占格。
- 不同网络在**同层**的占格不得重合，也不得正交相邻（对角相邻允许）。
- 每条网络有非负过孔上限 `via_limit`。
- 目标（字典序）：先最小化**总过孔数**，再最小化**同层移动总步数**。

## POST /route

### 请求体

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `width` | int | 网格宽，2～20 |
| `height` | int | 网格高，2～20 |
| `blocked` | array | 禁布格列表，元素为 `{"x", "y", "layer"}`，可省略 |
| `nets` | array | 1～6 条网络 |
| `nets[].name` | string | 可选名称，缺省自动命名 `N1`、`N2`… |
| `nets[].pads` | array | 恰好 2 个焊盘，各为 `{"x", "y", "layer"}` |
| `nets[].via_limit` | int | 非负整数，该网过孔数上限 |

坐标从 `0` 开始，`0 <= x < width`，`0 <= y < height`，`layer ∈ {0, 1}`。

### 响应体

| 字段 | 说明 |
| --- | --- |
| `status` | `optimal`：已证明最优；`feasible`：10 秒限时到、已有可行方案（未证明最优）；`infeasible`：已证明无解；`unknown`：10 秒限时到、尚未找到任何可行方案 |
| `solve_time_seconds` | 求解耗时（秒） |
| `total_vias` | 总过孔数（`infeasible`/`unknown` 时为 `null`） |
| `total_moves` | 同层移动总步数（`infeasible`/`unknown` 时为 `null`） |
| `nets[].path` | 该网从 `pads[0]` 到 `pads[1]` 的有序占格序列（含过孔两端的层切换） |
| `nets[].vias` | 该网过孔坐标列表（该坐标两层均被占用） |
| `nets[].via_count` / `nets[].move_count` | 该网过孔数 / 同层移动步数 |

求解时限为每次请求 **10 秒**（可用环境变量 `ROUTER_TIME_LIMIT` 覆盖，
见 `docker-compose.yml`）。只有完成证明时才返回 `optimal` 或 `infeasible`；
限时触发时按是否已找到可行方案区分 `feasible` 与 `unknown`。

### 实际请求与响应示例

下面是一个真实可复现的例子（8×8 板，第 0 层 `x=3` 整列禁布形成一堵墙，
N1 必须借道第 1 层；N2 在第 1 层直行，注意 N1 在第 1 层的走线段与 N2
保持了至少一格间距）：

```bash
curl -s -X POST http://localhost:8000/route \
  -H 'Content-Type: application/json' \
  -d '{
  "width": 8,
  "height": 8,
  "blocked": [
    {"x": 3, "y": 0, "layer": 0}, {"x": 3, "y": 1, "layer": 0},
    {"x": 3, "y": 2, "layer": 0}, {"x": 3, "y": 3, "layer": 0},
    {"x": 3, "y": 4, "layer": 0}, {"x": 3, "y": 5, "layer": 0},
    {"x": 3, "y": 6, "layer": 0}, {"x": 3, "y": 7, "layer": 0}
  ],
  "nets": [
    {"name": "N1", "pads": [{"x": 0, "y": 3, "layer": 0}, {"x": 7, "y": 3, "layer": 0}], "via_limit": 2},
    {"name": "N2", "pads": [{"x": 0, "y": 5, "layer": 1}, {"x": 7, "y": 5, "layer": 1}], "via_limit": 0}
  ]
}'
```

实际响应（`solve_time_seconds` 随机器略有浮动）：

```json
{
  "status": "optimal",
  "solve_time_seconds": 0.234,
  "total_vias": 2,
  "total_moves": 14,
  "nets": [
    {
      "name": "N1",
      "path": [
        {"x": 0, "y": 3, "layer": 0},
        {"x": 1, "y": 3, "layer": 0},
        {"x": 2, "y": 3, "layer": 0},
        {"x": 2, "y": 3, "layer": 1},
        {"x": 3, "y": 3, "layer": 1},
        {"x": 4, "y": 3, "layer": 1},
        {"x": 5, "y": 3, "layer": 1},
        {"x": 6, "y": 3, "layer": 1},
        {"x": 7, "y": 3, "layer": 1},
        {"x": 7, "y": 3, "layer": 0}
      ],
      "vias": [
        {"x": 2, "y": 3},
        {"x": 7, "y": 3}
      ],
      "via_count": 2,
      "move_count": 7
    },
    {
      "name": "N2",
      "path": [
        {"x": 0, "y": 5, "layer": 1},
        {"x": 1, "y": 5, "layer": 1},
        {"x": 2, "y": 5, "layer": 1},
        {"x": 3, "y": 5, "layer": 1},
        {"x": 4, "y": 5, "layer": 1},
        {"x": 5, "y": 5, "layer": 1},
        {"x": 6, "y": 5, "layer": 1},
        {"x": 7, "y": 5, "layer": 1}
      ],
      "vias": [],
      "via_count": 0,
      "move_count": 7
    }
  ]
}
```

路径中相邻两格 `layer` 相同表示一次同层移动，`layer` 不同（坐标不变）
表示一次过孔。

### 无解示例

两焊盘分处不同层但 `via_limit` 为 0，可证明无解：

```bash
curl -s -X POST http://localhost:8000/route \
  -H 'Content-Type: application/json' \
  -d '{
  "width": 4,
  "height": 4,
  "nets": [
    {"name": "N1", "pads": [{"x": 0, "y": 0, "layer": 0}, {"x": 3, "y": 3, "layer": 1}], "via_limit": 0}
  ]
}'
```

```json
{
  "status": "infeasible",
  "solve_time_seconds": 0.005,
  "total_vias": null,
  "total_moves": null,
  "nets": null
}
```

### 输入校验（HTTP 422）

以下非法请求返回 `422 Unprocessable Entity`（FastAPI/Pydantic 校验）：

- 网格宽高超出 2～20，或网络条数不在 1～6；
- 焊盘越界（`x`/`y` 超出网格或 `layer` 非 0/1）；
- 焊盘落在禁布格上；
- 重复焊盘（任意两焊盘的 `(x, y, layer)` 完全相同，含同一网内）；
- 过孔上限为负数或非整数；
- 禁布格越界。

示例（焊盘落在禁布格上）：

```bash
curl -s -X POST http://localhost:8000/route \
  -H 'Content-Type: application/json' \
  -d '{
  "width": 4, "height": 4,
  "blocked": [{"x": 1, "y": 1, "layer": 0}],
  "nets": [{"pads": [{"x": 1, "y": 1, "layer": 0}, {"x": 3, "y": 3, "layer": 0}], "via_limit": 1}]
}'
# HTTP 422
# {"detail":[{"type":"value_error","loc":["body"],
#   "msg":"Value error, net 0 pad (1, 1, layer 0) is on a blocked cell", ...}]}
```

## 实现要点

- **模型**（`app/solver.py`）：每条网络建模为从 `pads[0]` 到 `pads[1]` 的
  单位流。节点为两层上的自由格；弧为同层正交移动弧与过孔弧。除源/汇外
  每个节点入度=出度≤1，保证路径不重复占格；某网在同坐标两层均占格时
  过孔变量强制为 1（过孔占两层）。异网之间对同层每格及其正交邻格加
  互斥约束。
- **目标**：`min 4795 * 总过孔数 + 总移动步数`。权重大于最大可能步数
  （6 网 × 799 步 = 4794），因此严格等价于字典序（先过孔后步数）。
- **证明速度**：每网移动步数变量定义域下界直接取两焊盘曼哈顿距离、
  过孔变量下界按焊盘是否异层取 0/1，使根节点下界立即有效；另用一个
  快速顺序 Dijkstra 启发式把可行布线作为 hint 注入加速搜索。
- **限时语义**：CP-SAT `max_time_in_seconds = 10`。返回 `OPTIMAL` /
  `INFEASIBLE` 才分别报告 `optimal` / `infeasible`；限时到时有解报
  `feasible`、无解报 `unknown`。若求解器超时但启发式已找到经独立
  校验的完整合法布线，也按 `feasible` 返回该方案。
- **解校验**：`tests/test_router.py` 内含独立校验器，对返回的每条路径
  检查端点、占格不重复、同层正交移动、过孔占两层、禁布格、过孔上限、
  异网隔离及目标值一致性。

## 项目结构

```
app/
  main.py      # FastAPI 入口，POST /route、GET /health
  schemas.py   # Pydantic 请求/响应模型与 422 校验
  solver.py    # CP-SAT 联合建模、启发式 hint、结果提取
tests/
  test_router.py
Dockerfile
docker-compose.yml
requirements.txt
```
