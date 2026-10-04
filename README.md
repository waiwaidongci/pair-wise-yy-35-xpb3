# 职业辐射剂量与异常事件

合并监测读数，比较历史剂量并管理超限调查、医学随访与报告期限。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8312
```

默认端口为`8312`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/withdrawals`，撤回结案申请（radiation_officer），提交`closure_version`、`reason`、`planned_items`、`expected_version`
- `POST /api/items/{id}/withdrawals/{aid}/retry`，审计写入失败后沿用同一申请号重试
- `POST /api/items/{id}/withdrawals/{aid}/review`，撤回复核（health_physicist），复核后更新当前依据
- `POST /api/items/{id}/dose-corrections`，剂量更正（dosimetrist、health_physicist），结案事件需先撤回
- `GET /api/items/{id}/withdrawals`
- `GET /api/items/{id}/closure-snapshots`，原结案快照
- `GET /api/items/{id}/basis`，依据修订历史（结案、剂量更正、撤回复核）
- `GET /api/audit`

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平之比决定升级程度，超过阈值必须进入调查；更正剂量不能覆盖已确认审计记录。

## 撤回结案

结案时自动保存结案快照（结案版本、依据、已结事项清单）。radiation_officer提交撤回申请后事件回到`follow_up`，结案时已结事项重开，原结案快照仍可查询；申请携带按事件递增的修订号，并发提交时先到者占用修订号，后到者收到当前版本冲突。审计链写入失败时从结案快照恢复（事件保持结案、事项恢复已结），申请保留`pending`状态，可沿用同一申请号重试。补录记录或剂量更正后按当前数据重算优先级、期限与升级判断；health_physicist复核撤回申请后写入新的依据修订，其他角色提交复核返回越权。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
