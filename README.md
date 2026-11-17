# 工厂能耗区间结算服务（FastAPI + SQLite）

按设备收集累计电表读数与换表交接，按相邻读数生成带来源编号的区间电量。
换表后新表起始值不会被算成负用电：跨表区间只有在区间内存在**唯一匹配**的
换表交接时才分段结算，否则标记为“无法判定”（`energy_kwh = null`），绝不报零。

## 启动（Docker Compose）

```bash
docker compose up --build
```

- API 地址：<http://localhost:8000>
- Swagger 操作入口：<http://localhost:8000/docs>（ReDoc：`/redoc`，OpenAPI：`/openapi.json`）
- SQLite 数据持久化在 compose 卷 `energy-data`（容器内 `/data/energy.db`）
- 测试用故障注入：把环境变量 `FAIL_DEVICE` 设为某设备号，该设备的重建将始终在提交前失败

## 数据模型与规则

### 读数 `POST /readings`

```json
{
  "source_id": "R1",          // 来源编号
  "revision": 1,              // 递增修订号（首次为 1，之后必须正好 +1）
  "device_id": "DEV-A",       // 设备
  "ts": "2026-11-10T00:00:00Z", // UTC 时刻（必须带时区）
  "meter_no": "M-100",        // 表号
  "cumulative_kwh": 1000.0    // 累计 kWh（非负）
}
```

### 换表交接 `POST /handovers`

```json
{
  "source_id": "HV1", "revision": 1, "device_id": "DEV-A",
  "ts": "2026-11-10T02:00:00Z",
  "old_meter_no": "M-100", "old_final_kwh": 1100.0,
  "new_meter_no": "M-200", "new_initial_kwh": 2.0
}
```

### 修订规则（读数与交接一致）

- 同一 `source_id` 的修订号必须严格连续递增（1 → 2 → 3…）；
- 同 `source_id + revision` 再次提交且**内容完全相同**：幂等，返回 `idempotent=true`；
- 同 `source_id + revision` 内容不同：**409 拒绝**（`content conflict`）；
- 回退旧修订号或跳号：**409 拒绝**（`revision rollback` / `non-consecutive revision`）。
- 内容指纹为除 `source_id/revision` 外业务字段的 SHA-256，入库保留全部历史修订。

### 区间电量 `GET /intervals`

相邻读数两两生成区间，区间上附两端读数的来源编号与修订号（以及使用的交接来源）：

| 情形 | 结算方式 |
| --- | --- |
| 同表 | 后值 − 前值；差值为负 → `null`（`negative_difference`），不报零 |
| 跨表，区间内有唯一匹配交接（表号、设备、时刻均匹配） | `(旧表末值−前读数)+(后读数−新表初值)`，任一段为负 → `null` |
| 跨表但无交接 / 交接表号不匹配 / 交接不唯一 | `null`（`handover_missing` / `ambiguous_handover`） |
| 真实零差值 | 正常写 `0.0`，`undeterminable=false` |

查询参数：`device_id`（可选）、`status=current|stale|all`（默认 current）。

## 区间对账 `POST /reconciliations`

每班核对一台总表与若干分表在 UTC 闭区间 `[start_ts, end_ts]` 内的计量差额：

```json
{
  "total_device_id": "T",              // 总表设备号
  "sub_device_ids": ["A", "B"],        // 分表设备号（去重，顺序无关）
  "start_ts": "2026-01-01T00:00:00Z",  // UTC，含
  "end_ts": "2026-01-01T02:00:00Z",    // UTC，含，必须晚于 start_ts
  "tolerance_kwh": 5.0                 // 允许差额（非负）
}
```

**只有全部前置条件满足才发布结论**（`status=published`，HTTP 201）：

1. 每台参与设备都有 **现行** 区间完整覆盖时段：时段起点之前/起点处、终点之后/
   终点处各有可用读数作为边界，且边界同刻读数唯一（否则 `ambiguous_boundary`）。
   对账按设备自然的分段边界输出完整区间，不按窗口二次切分或按比例摊分；
   若窗口落在某区间内部，返回的是覆盖该窗口的完整区间（两端读数时刻）；
2. 各设备的分段边界必须与总表 **完全一致**（否则 `boundary_mismatch`）；
3. 每段电量均可判定：总表与各分表都不得出现 `energy_kwh=null`
   （倒退、缺交接、交接不唯一等都会使整次对账 `undeterminable`）；
4. 上述判据基于已提交的**现行**数据：分表与总表都从同一份已提交快照读取，
   绝不混用旧版本读数或 stale 行。

逐段返回（`segments[]`）：`start_ts/end_ts`、`total_kwh`、`sub_kwh`（各分表合计）、
`difference_kwh = total_kwh − sub_kwh`、`exceeded`（`|差额| > tolerance_kwh`，
恰好等于不算超限）、以及 `total_readings` / `sub_readings`（两端读数、设备号、
`source_id`、`revision`）与 `total_handover` / `sub_handovers`（本段实际使用的交接及修订号）。
顶层 `exceeded` 为任一段超限的汇总。

**任一项不满足则不发布部分结论**：整次请求返回 `status=blocked`、`segments=null`、
`exceeded=null`、`blocked_devices`（受影响设备）与 `blocked_details`
（`device_id` + `start_ts/end_ts/reason` 明细），`detail` 汇总原因，
典型原因：`incomplete_coverage`、`ambiguous_boundary`、`boundary_mismatch`、
`undeterminable`、`pending_rebuild`、`current_intervals_unavailable`。

### 结论的供数资格与更正

- 结论一旦发布即持久化，按 `POST` 返回的 `id` 可查询/追溯；
- **任一参与设备的读数或交接更正（新修订）一经受理**，所有依赖该设备的结论
  在受理的同一事务内立即变为 `superseded`，失去当前供数资格；
- 更正后重建失败（或服务重启）也不会让旧结论复活——它始终是 `superseded`；
- 只有 **重新对账** 才会生成新版本（同一组参数 `version` 递增），旧版本按
  `GET /reconciliations/{id}` 或 `status=all` 可追溯；
- 对账与更正交错时，新结论只读取已提交的现行数据，且重算/失效在
  `BEGIN IMMEDIATE` 事务内完成，不会与并发更正混合；
- 无关设备的结论不受影响，照常可查。

查询：

| 接口 | 说明 |
| --- | --- |
| `GET /reconciliations/{id}` | 按编号追溯（含 `superseded` 历史版本） |
| `GET /reconciliations` | 列表；默认只返回 `current`（published/blocked），`status=all` 查全部；支持 `total_device_id`、`sub_device_id`、`start_ts`、`end_ts`、`tolerance_kwh`、`limit` |
| `GET /reconciliations/latest` | 按同一组参数查当前结论；已失效/从未对账返回 404 |

> 注意：`latest` 与列表的 `status=current` 只返回仍有供数资格的结论；
> 已 `superseded` 的旧结论只能通过编号或 `status=all` 追溯。

## 迟到数据与重建的一致性保证

1. 读数/交接一旦**受理**，受影响设备的全部 `current` 区间在同一事务内立即
   置为 `stale`（失去当前供数资格，仍可按 `status=stale` 审计查询）。
2. 受理后自动按设备重建；也可 `POST /rebuild?device_id=...` 手动重试，
   或 `POST /rebuild`（默认只重建仍有 stale 区间的设备）。
3. 重建在单事务内完成：失败/进程崩溃时事务回滚，旧结果保持 stale，
   **不会复活为 current**；服务重启不自动重建，只把崩溃遗留的 running
   运行标记为 `interrupted`（见 `GET /rebuild-runs`）。
4. 重建按设备隔离：一个设备失败不影响其他设备，无关区间全程可查；
   重试可完成重建并恢复 current 供数。
5. 无法判定的区间以 `null` 表达，零值只表示实测零差值。

测试钩子：对提交或 `/rebuild` 加请求头 `X-Fail-Rebuild: true`，
对应设备的重建会在提交前失败，用于验证上述失败/重试场景。

## 本地开发与测试

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
pytest -q
```

对账自测覆盖：逐段差额/超限、跨表交接来源、缺段/边界不一致/无法判定/
待重建的受阻（不发布部分结论）、读数与交接更正导致结论失效、重建失败
不复活旧结论、无关设备不受影响、并发更正与对账不混用数据（`tests/test_reconciliation.py`）。

## 目录结构

```
app/            FastAPI 应用（models/database/service/main）
tests/          端到端自测（TestClient + 临时 SQLite 文件）：
                test_api.py 17 项读数/区间/重建；test_reconciliation.py 28 项区间对账
Dockerfile      python:3.11-slim 镜像，含 /health 健康检查
docker-compose.yml
```
