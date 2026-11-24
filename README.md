# 交换机出口 告警/相机图像 重放试算与时隙登记 API

产线告警与相机图像共用同一台交换机出口。本服务提供两件事：

1. **重放试算**（`POST /replay`）：用离散事件仿真重放一段时间内的帧发送过程，
   供网管在**改配前试算**——调整周期与告警预留时长后，哪些告警会超时。
2. **时隙登记**（`POST/GET /lines/{line_id}/slots`）：网管拿到发送时隙后，
   为单条产线的 1~6 条告警流登记首发时刻与发送间隔，整组求解、持久化。

所有时间均为**整数毫秒**，时间轴从 `0` 开始，仿真区间为半开区间 `[0, replay_end_ms)`。

## 调度规则

1. 时间轴按周期划分，每个周期内：
   - 前段 `[周期起点, 周期起点 + reserve_ms)` 为**告警时段**，只允许发告警帧；
   - 其余为**图像时段**，只允许发图像帧。
   - `reserve=0` 表示没有告警时段；`reserve=period` 表示没有图像时段。
2. 出口同一时刻只发一帧；同类帧严格按到达顺序发送（同时到达按请求中的先后）。
3. **整帧必须落在本类时段内**才会开始发送：`now + busy_ms <= 时段终点`。
   队首帧放不进当前时段时，同队后续帧不许插队，本时段剩余时间空转，等下一个同类时段。
4. 可在指定时刻换一套周期/预留时长。切换只在**周期边界**生效：
   - 到点若出口正在发帧，等该帧发完（走到下一个周期边界）再切换；
   - 到点若出口空闲，等当前周期结束的边界再切换；
   - 排队帧保留；新周期从实际生效时刻重新对齐（生效时刻即新周期的第 0 毫秒）。
5. 告警从到达开始计时：发完即停止（耗时 = 发送终点 − 到达）；未发完（发送中或排队）算到重放终点
   （耗时 = `replay_end_ms − 到达`）。耗时**严格大于**告警时限即超时。

帧在终点时刻的状态：

| status | 含义 |
|---|---|
| `sent` | 已发完，返回 `start_ms` / `end_ms` |
| `in_progress` | 终点时仍在发送（未发完），有 `start_ms`，`end_ms=null` |
| `queued` | 已到达但排队未发 |
| `not_arrived` | 到达时刻不早于重放终点，未进入仿真 |

## 运行

### Docker Compose（推荐交付方式）

```bash
docker compose up --build
# 打开浏览器
#   http://localhost:8000/docs
```

`/docs` 内置两组可直接 “Try it out” 的示例：

- `/replay`：含换配 / 不换配；
- `/lines/{line_id}/slots`：首次登记、整组改选、拥挤竞争者（可连点制造并发争用）。

时隙方案持久化在 SQLite（容器内 `/app/data/slots.db`，挂在命名卷 `slot-data` 上），
`docker compose restart` 或 `down` + `up`（不带 `-v`）之后仍可按产线查询生效方案。

### 本地（无 Docker 时）

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 时隙库默认写到 ./data/slots.db，可用 SLOT_DB_PATH 覆盖
```

## 接口

### `POST /replay`

请求字段：

| 字段 | 说明 |
|---|---|
| `period_ms` / `reserve_ms` | 初始周期、每周期告警预留时长（`0 <= reserve <= period`） |
| `frames[]` | 每帧：`type`（`alarm`/`image`）、`arrival_ms`、`busy_ms`、可选 `id`（缺省 F1、F2…） |
| `alarm_deadline_ms` | 告警时限，耗时严格大于该值判超时 |
| `replay_end_ms` | 重放终点 |
| `switch_at_ms` + `new_period_ms` + `new_reserve_ms` | 可选，三者须同时给出 |

响应返回：`switch`（计划/实际切换时刻与 `switched`/`not_switched`/`not_requested` 状态）、
终点时刻实际生效的周期与预留、逐帧发送起止与终点状态、告警计时与超时标记、汇总统计。

另有 `GET /health` 健康检查。

### `POST /lines/{line_id}/slots` — 登记或替换一条产线的时隙

首次成功登记时确定出口的 `period_ms` 与 `reserve_ms`，之后所有产线共用这套配置。
每次请求提交**一条产线**的 1~6 条告警流，整组一起替换；整组全部有解才生效。

请求字段：

| 字段 | 说明 |
|---|---|
| `period_ms` / `reserve_ms` | 出口周期与每周期告警预留时长。首次登记必须同时提供；之后可省略，若提供必须与已确定值一致（否则 409） |
| `flows[]` | 1~6 条流，按请求顺序返回 |
| `flows[].flow_id` | 流标识，同一请求内唯一 |
| `flows[].busy_ms` | 整帧占线时长（整数毫秒，>=1） |
| `flows[].repeat_every_periods` | 重复间隔，以周期计（>=1） |
| `flows[].start_points[]` | 1~6 个可用起点：`{first_period, offset_ms}`，首发周期号（0 起）+ 周期内偏移 |

约束与行为：

- 每条流从候选起点中选一个；首帧绝对时刻为 `first_period * period_ms + offset_ms`，
  之后每 `repeat_every_periods` 个周期重复。
- **整帧落在告警预留段**：`offset_ms + busy_ms <= reserve_ms`，否则该起点不可用。
- **同一周期内帧不得重叠**；不同周期序列是否可能同周期用同余判断
  （`first_a ≡ first_b (mod gcd(repeat_a, repeat_b))`），**不逐周期展开**。
- 求解为带剪枝的回溯：每条流尽量选最早的可行起点；候选受限的流优先尝试，必要时回退整组改选。
- **其他产线时隙原位保留**，作为不可移动的占用参与求解。
- **并发登记**在 SQLite `BEGIN IMMEDIATE` 写事务内串行化（读他人方案 + 求解 + 写入原子），
  多 worker/多进程同样安全。
- 无解返回 **409**，原登记**保持不变**；响应头无副作用。

成功响应（200）在 `LinePlan` 之外带 `applied=true` 与 `created`，每条流返回选中的
`start_point` / `start_point_index` 及首帧 `first_start_ms` / `first_end_ms`、
首发周期的 `reserve_start_ms` / `reserve_end_ms`。

`GET /lines/{line_id}/slots` 查询某产线当前生效方案（未登记 404）；
`GET /slots` 返回出口配置与全部产线方案总览。

#### 登记示例

```json
{
  "period_ms": 100,
  "reserve_ms": 40,
  "flows": [
    {
      "flow_id": "A1",
      "busy_ms": 20,
      "repeat_every_periods": 1,
      "start_points": [
        {"first_period": 0, "offset_ms": 0},
        {"first_period": 0, "offset_ms": 20}
      ]
    },
    {
      "flow_id": "A2",
      "busy_ms": 15,
      "repeat_every_periods": 1,
      "start_points": [
        {"first_period": 0, "offset_ms": 0},
        {"first_period": 0, "offset_ms": 20}
      ]
    }
  ]
}
```

上述请求返回 A1 选 `(0, 0)`、A2 选 `(0, 20)`：A1 占 `[0,20)`，A2 占 `[20,35)`，
都在 40ms 预留段内且不重叠。

### 重放请求示例

```json
{
  "period_ms": 100,
  "reserve_ms": 40,
  "alarm_deadline_ms": 80,
  "replay_end_ms": 320,
  "frames": [
    {"id": "A1", "type": "alarm", "arrival_ms": 0,   "busy_ms": 30},
    {"id": "A2", "type": "alarm", "arrival_ms": 5,   "busy_ms": 60},
    {"id": "I1", "type": "image", "arrival_ms": 10,  "busy_ms": 50},
    {"id": "A3", "type": "alarm", "arrival_ms": 120, "busy_ms": 25},
    {"id": "I2", "type": "image", "arrival_ms": 130, "busy_ms": 70}
  ],
  "switch_at_ms": 150,
  "new_period_ms": 120,
  "new_reserve_ms": 60
}
```

## 目录结构

```
app/
  main.py         FastAPI 入口（/replay、时隙路由、/health、/docs）
  models.py       重放请求/响应模型与交叉字段校验
  replay.py       离散事件仿真引擎
  slot_models.py  时隙登记请求/响应模型（1~6 条流 × 1~6 个起点）
  slot_solver.py  整组时隙求解（CRT 判周期相交 + 回溯）
  slot_store.py   SQLite 持久化、首次配置固定、并发串行化
  slot_api.py     时隙登记路由与 /docs 示例
tests/
  test_replay.py  18 个重放用例
  test_slots.py   19 个时隙用例（求解/持久化/并发/HTTP）
Dockerfile / docker-compose.yml / requirements.txt
```

## 自测

```bash
pip install pytest httpx
pytest -q
```

覆盖重点：

- **整帧放不进本类时段**：队首阻塞（后续同类帧不插队）、放不进的帧持续 `queued` 且不影响异类、
  换配扩大预留后队首先于后续帧发出；
- **发帧中改配**：换配时刻落在发送区间内时，等帧发完的周期边界才切换、排队帧保留、
  新周期从生效时刻重对齐；空闲时换配等当前周期结束；恰好落在边界则当刻生效；
  被推迟到终点之后则 `not_switched`；
- 告警超时边界（等于时限不超时、严格大于才超时）、`in_progress`/`queued` 计时到终点；
- `reserve=0`、`reserve=period`、到达时刻等于终点、缺省 id、参数校验 422；
- 发送区间互不重叠、区间长度等于占线时长等不变量；
- 大终点（10^7 ms）快速收敛，不随周期数空转；
- **时隙求解**：整帧不落入预留段直接无解、最早就近选点、回溯让位（含重复间隔不同）、
  CRT 判定「周期序列永不相交」与「相交必须错开偏移」、其他产线占用不可移动；
  另有 300 组随机用例与「逐周期展开」暴力判定交叉比对（有无解判定完全一致、解本身合法）；
- **时隙持久化**：首次登记固定出口配置、之后不一致返回 409、无解不覆盖原登记、
  整组替换原子生效、其他产线方案逐字节不变、新实例（模拟重启）仍可查询；
- **并发**：8 线程 / 6 HTTP 线程同时争抢同一预留段，仅容量允许的数目成功，
  最终所有生效方案两两不冲突。

实测（本地 venv，FastAPI 0.165 / Pydantic 2.14 / uvicorn 0.54）：**37 passed**，
全量约 2.5s。真实 uvicorn（`--workers 4`，多进程共享同一 SQLite）起服务后实测：

- `/health`、`/docs`、`/openapi.json` 均 200，非法请求 422；
- 12 个并发登记争抢 `[0,40)` 预留段（1 个种子 + 每个竞争者需 10ms 槽）：
  恰好 3 个返回 200、9 个返回 409，`/slots` 中 4 条产线两两不重叠；
- 整组改选：首选起点被同伴占用时回退改选（Q1 从 0 让到 20，Q2 保住 0），
  重复提交同一组结果稳定；替换后其他产线偏移不变；
- 无解请求返回 409 且再次查询仍是原方案；
- 进程重启后按产线查询仍返回同一方案。

## 余项 / 约定说明

- 本交付环境无 Docker 守护进程，`docker compose build/up` 仅做了 compose YAML 与 Dockerfile 静态核对，
  未实机构建；在有 Docker 的机器上执行上文命令即可。功能验证用等价的本地 uvicorn（`--workers 4`）完成。
- “切换只在周期边界生效”是对“到点若有帧在发，等它发完再切换”的一致化推广：
  出口空闲时也等到当前周期结束，避免在时段中间凭空产生新的时段边界。
- 仿真为纯函数式计算、无外部状态，单请求耗时只与帧数量和事件数相关，与 `replay_end_ms` 大小无关。
- 时隙求解复杂度与周期数无关（只做两两同余 + 区间判断），上界 6 条流 × 6 起点；
  候选起点按首帧绝对时刻升序尝试，因此结果是确定性的“尽早”方案，而非全局最优
  （公平性/负载均衡未建模）。
- 时隙方案按“产线整组”替换：修改一条产线的流集合时，该产线原方案整体被新方案取代，
  不会保留旧流；其他产线不受影响。
- SQLite 单文件配合 WAL 足以支撑本场景；若将来改为多主机部署，需要换成集中式数据库
  （并发控制点已收敛在 `SlotStore.register` 的单个写事务内）。
- 尚未提供删除产线的接口；如需下线产线，可后续补 `DELETE /lines/{line_id}/slots`。
