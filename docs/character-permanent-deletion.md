# 角色永久删除

本功能从 `v1.7.0-luoluo.2` / `6ce2a1f8973d4700a00a3272a7e0a86263ed0685` 增量开发，修改前检查点为 `codex/checkpoint-2026-09-26-before-model-change`。不修改现有标签或运行时版本，不代表正式发布、部署或生产验收。

## 页面使用与数据范围

开启角色隔离后，在 `/character-manager` 的目标角色卡点击“永久删除”，输入完全一致的角色 ID。已停用角色同样可删除。删除过程中不可打开该角色面板或改名；只有服务器确认成功后才移出页面。失败会重新读取注册状态，保留继续删除/重试入口。

此操作停止该角色 worker，然后删除其独立 PostgreSQL 数据库：记忆（含锁定、归档和项目记忆）、记忆向量、Dream 场景与场景向量、项目及文件片段向量、消息与原始对话、压缩摘要、画像、日历、提醒、配置/供应商信息、关系边、会话归属和 `float_memory_events` 收据都在范围内。进程停止也清除该角色的运行时缓存和后台任务。

`default` 所在库还保存整个角色注册表，继续禁止删除。其他角色库、部署 `.env`、默认人设文件、外部备份及 Float 本地内容不受本操作修改。本功能不调用 Float 接口；旧调用对删除中角色收到 409，对已删除角色收到 410，绝不回退 default。

页面的完整 ID 确认用于防误操作，不是认证。当前服务继续依赖部署层的可信管理入口/认证代理，没有新增用户级权限系统。

## 接口与状态

| 操作 | 合同 |
| --- | --- |
| 永久删除 | `POST /characters/{id}/purge`，JSON 只能包含 `{"confirm_character_id":"目标ID"}` |
| 成功或重复请求已删角色 | 200，`{"status":"deleted","data_retained":false}` |
| 确认缺失/不匹配/额外字段 | 400 `character_purge_confirmation_required` |
| 路径、请求头、正文角色冲突 | 409 `character_mismatch`，先于删除处理 |
| 未知角色 / default | 404 `character_not_found` / 409 `default_character_protected` |
| 数据库身份异常 | 409 `character_database_identity_invalid`，拒绝删库 |
| 删除未完成 | 503 `character_purge_failed`；修复原因后可重试 |
| 旧停用接口 | `DELETE /characters/{id}` 仍保留数据，返回 `disabled` / `data_retained:true` |

注册状态从 `active` 或 `disabled` 进入 `deleting`，完成后进入 `deleted`。`GET /characters` 保留最小墓碑以防旧客户端重复使用 ID；页面隐藏 `deleted`。删除中的条目可附带脱敏的 `deletion_error`，不公开数据库名称、连接串或原始异常。已永久删除记录的显示名称清空。创建同一 ID 仍返回 409，已删除记录不占用活动角色名额。

## 中断恢复与数据库保护

1. 开始删除前持久记录 `deleting`，立即拒绝新业务请求。等待同角色启动/停止锁并再次检查状态；不会等待无限 SSE 自然结束。
2. 停止并等待 worker 退出；必要时强制结束该角色进程。无法确认停止时不删库。
3. 从注册表取得服务端生成的库名，校验 UUID 格式、唯一归属、原数据库 OID、数据库 owner，排除 default、注册库和模板库。请求不能指定库名。
4. 独立维护连接禁止目标库的新连接，再删除目标库及剩余连接。DDL 有超时，不占用注册表锁/心跳连接。确认库不存在后才记录 `deleted`。
5. 删库和状态回执无法放进同一个 PostgreSQL 事务，因此各阶段都可重试。若删库已完成但回执失败，重试补写墓碑；若同名数据库已被重新创建，OID 不一致会阻止误删。
6. HTTP 调用断开不会取消已经开始的清理；supervisor 退出时会取消并收尾任务。重启或心跳继续处理 `deleting`，单个角色失败不阻塞其他活动角色启动或续租。旧停用操作也不能覆盖删除状态。

首次启动幂等扩展注册表状态约束，增加原数据库 OID、删除错误码和完成时间。存量活动/停用角色绑定其现有库 OID；不会在恢复删除时重新绑定 OID。新增角色建库时记录 OID。迁移不改写已有记忆、向量或配置。

部署账号需拥有它创建的角色数据库，并具有终止该库残留连接的权限。所有权不匹配会拒绝；删库权限、连接或数据库条件导致失败时，保持删除中，修复后重试。不要通过修改注册表库名/OID来绕过身份检查。

## 数据与源码回退

永久删除不是归档，源码检查点不能恢复角色数据。本操作不自动创建备份，也不清理独立外部备份。需要恢复能力时，应在删除前按多角色维护文档停写并验证完整数据库备份；普通 Kiwi ZIP 不包含完整向量、派生表和事件收据，不能当作完整恢复点。

升级前备份注册库及各角色库。存在 `deleting` 时不要直接回退旧程序；先完成清理或在隔离副本恢复一致备份并验证。旧标签不认识新增删除状态，不能作为本功能的恢复执行器。对已删角色的数据恢复必须从独立备份设计恢复流程，不得仅将注册状态改回 active。

跨集群恢复数据库会改变 OID。本功能会拒绝使用旧 OID 执行永久删除；恢复时须由管理员核实完整角色—数据库映射并安排受控重新绑定，不能自动按同名库放行。本轮不提供跨集群恢复工具。

## 验证入口

- `python scripts/test_character_purge.py`：HTTP 确认与身份合同、worker 停止顺序、失败/取消/并发重试。
- `node scripts/test_character_manager.mjs`：执行真实页面内联脚本的 DOM 行为，使用模拟 HTTP。
- `python scripts/test_character_purge_postgres.py`：显式 `KIWI_TEST_DATABASE_URL` 的一次性本地 PostgreSQL、真实 worker、模拟模型；验证数据库消失及其他角色内容不变。
- 既有角色隔离、进程生命周期、数据库、Float 记忆回归、`python -m compileall -q .`、`git diff --check` 一并执行。

## 2026-09-27 本地执行结果

环境为 Windows、Python 3.12、全新一次性 PostgreSQL 16.15；独立集群只接受本机连接，测试脚本仅创建/删除自己的随机数据库。模型和 embedding 供应商均为本地替身。

| 验证 | 结果与证据边界 |
| --- | --- |
| 先测旧实现 | 后端 10 tests：12 个失败断言、4 errors，命中缺少 purge、错误代理与确认缺失；旧页面仅 1/16 通过 |
| 新 HTTP / 生命周期 | `test_character_purge.py` 15 tests 通过，含重复请求交错、客户端取消、supervisor 关闭、心跳故障隔离 |
| 页面行为 | `test_character_manager.mjs` 16/16 通过；执行真实 inline 脚本，DOM/HTTP 为模拟，不是浏览器人工验收 |
| 新真实库 / worker | `test_character_purge_postgres.py` 43 guards 通过；A 整库消失，B/default 三类向量与相关表逐行快照相同，真实 SSE/进程退出、旧 schema 迁移、失败重试、重启恢复、同名新 OID 保护 |
| 既有数据库守卫 | `test_kiwi_safety_sync.py` 178 guards；`test_character_postgres.py` 61 guards；`test_character_extraction.py` 8 guards；`test_character_scope_regressions.py` 全部通过 |
| Float 兼容性 | `test_float_memory_api.py <Float checkout>` 通过真实 TCP + 现有 Float TS 适配器联调；`test_float_dream.py` F1–F7 通过。未修改 Float 源码 |
| 其他既有回归 | CI 普通回归所列 Python/Node 脚本全部 exit 0，包含角色隔离 22 tests、生命周期 8 tests、更新保护 7 tests，以及工具、流式、日历、MCP、管理面板等既有脚本 |
| PREP 兼容 | `test_prep_framework_compat.py` 通过；`test_kiwi_prep_01.py` 15 tests，1 项 Windows 明确跳过；最小 POSIX PATH、额外 awk 变体和真实 Compose 配置需 Linux CI |
| 负向变异 | 临时副本移除 OID 比对后，真库测试在“不得删除不相关库”断言失败；恢复后的原实现通过。前端移除精确 ID 校验也被测试捕获。变异未改工作树源码 |
| 清理 / 静态检查 | 各真库脚本 SQL 验证其随机数据库全部清理；`python -m compileall -q .`、`git diff --check` 通过 |

独立审查未发现可复现 P1/P2。GitHub CI 的 Linux、Docker 构建和依赖审计结果以本次 Draft PR 的 Checks 为准，不由本地通过结果代替。未做真实模型调用、真实浏览器人工操作、生产部署、生产删除或完整跨集群备份恢复演练；本记录是功能阶段验证，不是正式发布验收。
