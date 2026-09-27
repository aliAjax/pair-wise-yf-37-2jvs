# 传染病暴发调查与接触网络

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8303`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

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
python3 app.py --db ./data.db --port 8303
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `case`：病例和调查状态；`contact`：接触者随访。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。
- `GET /api/cases/summary`：每个病例的接触人数、逾期人数（待随访且已过观察截止日）和最近观察截止日。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 随访自动化

- 病例`lab_positive`确认时，已登记的接触者在同一事务中自动转为`following`（待随访），观察截止日为`exposure_start`+14天；中途出错则病例状态与随访名单整体回滚。
- 同一病例下同一`person_id`重复登记只保留一条，返回已存在的记录。
- 病例确认后补录的接触者创建时直接进入随访名单并计算观察截止日。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

病例关联和时间窗口是调查辅助规则，不替代公共卫生部门的流行病学判断。
