# float 独立记忆 API（本地开发）

2026-09-19，基于 kiwi 本地提交 `51ed818a03ae9e85e4c9891bc3c5f1f1c852e425`，分支 `codex/float-memory-api`；配套 float 基于 `59dfa54a73f746a16c0c8d1ca743e8e0afc490d8`，分支 `codex/kiwi-memory-adapter`。本轮接续中断任务已有工作，未覆盖本地、推送、发布、部署或接触生产数据。远程 GitHub 查询连接失败，未声称与远程同步。完成源码由本地提交固定，用 `git log -1 --format=%H` 读取。

原角色隔离快照不具备通用跨应用事件收据，所以新增 `float_memory_api.py`，由 `main.py` 注册路由、`character_boundary.initialize_character_tables` 初始化附加表；没有重构原聊天网关。

## 合同

仅支持角色 supervisor 路径 `/characters/{id}/memory-adapter/*`，worker 仍固定进程/独立库。未启用角色模式或角色记忆已关闭返回 409。角色存在性、停用、身份冲突由既有 gateway/boundary 处理；外部鉴权仍由已有认证代理负责。不要把角色 ID 当作权限凭据。

| 方法/后缀 | 输入 | 成功结果 |
| --- | --- | --- |
| GET `/capabilities` | 无 | `contract: float-memory-v1`、`character_id`、`idempotent_events`、`global_only`、`group_session_recall` |
| POST `/events` | `event_id`、`session_id`、`message_id`、`source`、`occurred_at`、`content`、`visibility: private/group`、可选 `importance:1..10` | `status:stored/duplicate`、`event_id`、`memory_id` |
| GET `/recall` | `q`、`limit`（1–100）、可选 `group_session_id` | `memories:[{id,content,importance,created_at}]` |

事件 ID、session/message ID 最长 256，source 最长 64，occurred_at 最长 64，正文最长 30000。拒绝未知字段。正文摘要按规范化 Pydantic 数据计算 SHA-256；同事件 ID 不同内容返回 409。数据库事务内 advisory lock 和收据主键保证同角色并发幂等；不同角色可使用相同事件/消息 ID。

本次直接保存 float 已确认的可见经历为 `source=float_event` 记忆碎片，不把内部辅助模型请求写为真实互动。不走聊天网关二次自动记录、不创建聊天会话、不把 `project_id` 当作角色。原模型/TTS/工具及流式输出仍由 float 直接调用原供应商。

普通召回使用角色内全局关键词/向量搜索或最近记忆，排除项目；当前调用 `track_recall=False`，不改变召回热度。群聊查询仅返回指定 session 的 group 事件对应、尚有效且未被 Dream 消化/删除的原记忆，排除私人、项目与派生内容。float 再取当前参与者共同拥有的内容，避免群聊聚合私人记忆。

编辑/删除复用角色前缀下既有 `PUT/DELETE /debug/memories/{memory_id}`；保持锁定保护。返回的数值 ID 只在当前角色内有效。

## 升级、备份和回退

worker 启动时 `CREATE TABLE IF NOT EXISTS float_memory_events`，存储事件 ID、摘要、session/message/source/time/visibility 和 memory_id。没有清空、复制或重写旧记忆。与记忆插入同事务，重复初始化安全。

删除记忆不删除收据：丢失响应后重试返回 duplicate，不复活旧条目。收据没有外键级联删除，也不作为聊天原文备份；完整 `pg_dump` 需要保留此表。现有 kiwi 管理界面导出包不等于包含收据的完整数据库备份，跨库备份继续按角色隔离文档执行。

先在副本验证再升级；本轮未升级任何真实库。旧代码可忽略附加表，但旧版本没有该 API；回退前将 float 切回内置记忆并同步微信运行包。不要删除角色库、注册表或收据来做回退。正式发布、生产恢复与性能验收未执行。

## 配套用法与验证

按现有角色隔离文档配置 PostgreSQL、供应商、`MEMORY_ENABLED=true`、`KIWI_CHARACTER_ISOLATION=true`，运行 `python character_gateway.py`（默认 8080）。float 设置 → 记忆后端填写根地址，为每个人物创建/绑定独立角色。浏览器凭据不进入角色卡、备份或微信同步包，微信代理 token 单独配置 `KIWI_MEMORY_TOKEN`。

```powershell
# 仅接受独立的 localhost 测试实例；脚本创建随机库并在 finally 清理
$env:KIWI_TEST_DATABASE_URL = 'postgresql://<test-user>:<test-password>@127.0.0.1:<test-port>/postgres'
python scripts/test_float_memory_api.py 'C:\Users\C.C\Desktop\ai-virtual-phone-main'
python scripts/test_character_postgres.py
python scripts/test_kiwi_safety_sync.py
python -m compileall -q .
```

2026-09-19 实际 HTTP + 生产 float TypeScript 适配 + 真实 supervisor/worker/PostgreSQL 联调通过，SQL 证明随机测试库清理完毕。覆盖角色隔离、跨会话、同名改名、并发去重、内容冲突、群聊 session、非聊天事件、项目排除、关闭本地核心注入、编辑/删除和删除后重试。供应商 HTTP 为测试替身，没有请求真实模型。

原角色 PostgreSQL 61 项守卫、S1–S6 等累计 178 项 PostgreSQL 守卫通过。执行 CI 中 22 个 Python 脚本、3 个 JS 脚本及 `compileall`，整体退出码 0。PREP 应用守卫 15 项中有 1 项 Linux PATH 矩阵跳过，脚本另报告本地 jq/可选 awk/真实 Compose 检查受阻；Docker 构建、依赖审计和 CI 专用变异脚本未执行。不是 GitHub CI 或正式发布验收。

float 专项还测试了模拟浏览器保存与重载、鉴权/网络/版本失败、请求取消/后端切换、持久事件重试、工具/草稿过滤、微信收据与角色归属。微信/Supabase 为替身，实际浏览器端、多设备、真实供应商、完整多库备份恢复、Docker 与长期容量尚需独立验收。

范围限制：float 当前管理角色全局记忆，不管理 kiwi 项目/画像/日历页面；无自动批量旧数据迁移。微信 kiwi 模式扫描积压云消息会随历史增加流量，浏览器收据未自动裁剪。服务没有新增用户权限系统，仍属于可信客户端的角色隔离。
