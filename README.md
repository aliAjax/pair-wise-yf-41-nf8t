# 地震台网事件编目与修订

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8307`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则、失败场景与联动测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8307
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象与依据

- `station`：观测台站。
- `event`：地震事件。事件数据中的 `reports` 是**当前采用的报告集合**，每台站只保留最新一版；
  `report_set_version` 是这份依据的版本号，`station_count` 按不同台站去重计数。
- `report`：台站观测报告实体（`received` / `superseded`）。旧版报告被新版取代后不删除，
  状态置为 `superseded` 留痕。

事件状态机：

```
candidate ──associate──▶ associated ──review──▶ reviewed ──publish──▶ published
     ▲                       ▲                                          │
     └──── 补报/对账退回 ────┴────────◀── pending_review ◀── reconcile 不符
                                                                 │
                                          reconcile 相符 ─▶ reconciled ──withdraw──▶ withdrawn
```

## 联动规则

1. **同一份依据**：事件、台站、观测报告都挂在事件的 `report_set_version` 上。补报在
   `BEGIN IMMEDIATE` 单事务内同时更新事件与报告实体，乐观锁保证同一事件的并发补报
   只认最新一版报告集合；`expected_version` 过期的补报返回 `409 Conflict`。
2. **补报使旧复核失效**：报告一更新，`reviewed` / `published` / `reconciled` 状态下
   先到的复核结论立即失效，事件退回 `pending_review`，按最新报告集合自动重算震级
   （各台站最新振幅的中位数）。旧结论归档到事件 `data.conclusion_history` 并写
   `invalidate_review` 审计记录，留痕可查。
3. **台站越权拒绝**：`station` 角色只能为归属自己的台站（台站实体的 `created_by`）
   补报；补别的台站或冒领已注册台站代号一律 `403 PermissionDenied`，且不留待重试。
4. **失败待重试**：补报先落 `pending_reports` 表，再尝试写入事件。写入失败（如版本冲突）
   时记录 `attempts/last_error` 并保留；重试按 `ref` 幂等，同一台站旧版退出采用集合，
   不重复计入台站数。
5. **发布对账**：发布后由复核方执行 `reconcile`，核对通信编号与报告集合版本。
   两者都一致才进入 `reconciled`；任一不符，一律退回 `pending_review` 并归档旧结论。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询（`stations` / `events` / `reports`），可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交动作：
  - `{"action":"supplement","data":{"station_code":"STA-1","amplitude":5.0,"ref":"唯一编号"},"expected_version":数字}`
    台站补报（`ref` 用于失败重试与幂等重放，也可用 `Idempotency-Key` 头）。
  - `{"action":"retry_pending","data":{"refs":["..."]}}` 重试未完成的报告。
  - `{"action":"reconcile","data":{"communication_id":"...","report_set_version":3}}` 发布后对账。
  - 其余动作：`associate` / `review` / `publish` / `withdraw`、台站 `online` / `offline`。
- `GET /api/pending`、`GET /api/pending/<event_id>`：查询待重试报告。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应；
自动震级为各台站最新振幅的中位数，人工复核震级会记录 `magnitude_basis: manual`。
