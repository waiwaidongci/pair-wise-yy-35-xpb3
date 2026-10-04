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
- `POST /api/items/{id}/withdrawal`，撤回结案申请（结案版本、撤回原因、拟补事项）
- `GET /api/items/{id}/withdrawal`，撤回申请列表
- `GET /api/items/{id}/withdrawal/{app_id}`，撤回申请详情（含原结案快照）
- `POST /api/items/{id}/withdrawal/{app_id}/submit`，辐射防护员提交（修订号乐观锁，后到者收到`current_revision`）
- `POST /api/items/{id}/withdrawal/{app_id}/review`，卫生物理师复核后更新依据（其他角色返回越权）
- `POST /api/items/{id}/dose-correction`，剂量更正（随访中，重算优先级/期限/升级判断）
- `GET /api/audit`

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平之比决定升级程度，超过阈值必须进入调查；更正剂量不能覆盖已确认审计记录。

撤回结案为剂量事件下的受控复核：申请人填写结案版本、撤回原因和拟补事项后生成申请号并冻结原结案快照；辐射防护员提交后事件回到随访，拟补事项重开为未结事项，原结案快照可查。两人同时提交时先到者占用修订号，后到者收到当前版本。审计链写入失败则从结案快照恢复、沿用同一申请号重试，剂量事件保持结案。补录记录或剂量更正后当前依据变化，优先级、期限和升级判断即时重算，但须经卫生物理师复核后才更新依据，其他角色提交返回越权。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
