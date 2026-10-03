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
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8307
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `station`：观测台站；`event`：地震事件及其多个修订版本；观测报告内联在事件的 `reports` 集合中。
- 事件维护 `report_version`（报告集合版本）、`review`（当前复核结论）和 `publication`（发布记录）。补报会令 `report_version` 加一，旧复核结论立即作废，震级在复核时由报告振幅中位数（`magnitude_median`）重算；无振幅时则要求人工录入震级。
- 补报（`supplement`）走乐观锁：请求需带 `expected_version`，并发补报只认最新一版，过期版本返回 `409 Conflict`。写入失败的报告进入待重试队列（`report_intake`），重试按台站 upsert，台站数不重复计入。
- 台站角色只能补本报台站的报告（按台站 `created_by` 校验），越权补报直接 `403`。
- 发布后通过 `reconcile` 与通信编号对账：事件当前 `report_version` 与发布记录不一致时，一律退回待复核（`associated`）。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
  - 事件支持 `associate` / `review` / `publish` / `revise` / `withdraw` / `supplement` / `reconcile`。
  - `supplement` 的 `data.reports` 为补报列表；`reconcile` 的 `data.communication_id` 为通信编号。
- `GET /api/audit`：读取审计记录，旧复核结论与对账结果均留痕可查。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
