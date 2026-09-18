# 第二轮角色隔离审查修复

日期：2026-09-18。本记录是本地阶段验证，不是发布验收或生产迁移记录。

- 上游基线：`b01a0c506f4f10f90f30d408c0291f16720f0718`，上游版本仍为 1.7.0。
- 本轮修复前提交：`2becca3947b7a493a4a5b11ad576c13a1f721f90`。
- 分支：`codex/character-isolation`。本记录与修复一起提交；未推送、部署或修改 float。
- 前一轮七项修复的历史证据保留在 [原记录](character-isolation-review-fixes.md)。

## 修改结果

| 审查问题 | 本轮处理 |
| --- | --- |
| 项目原文进入全局提取 | 角色 worker 的最近对话查询在 LIMIT 前限定已知全局归属。自动和手动提取共用出口，正文/来源版本/重置世代仍来自同一 SQL 快照。归属未知与项目原文保持存储；旧单角色读取语义保留。 |
| 编码面板地址回退 default | 面板 URL 解码一次后校验；非法或缺失角色、编码分隔符、不支持的路径立即拒绝。请求、下载、SSE、向导、标题和管理链接共用解析结果。 |
| Dream 标记绕过项目限制 | 非流式、普通流式、工具流式按请求项目快照检查 Dream 权限；项目标记不启动任务、不发送 ev_dream。断连也不绕过，全局及旧单角色行为保留。 |
| 长连接挤占续租连接 | 健康检查与启动探测使用独立 HTTP 客户端，业务池的 100 个长连接不阻塞续租。 |
| 注册表异常永久终止心跳 | 可恢复查询错误/超时会重试。连接断开或心跳任务异常结束由监督任务处理：停止 worker，关闭客户端和注册表，然后退出服务。断连不自动重连旧 supervisor，避免丢失 advisory lock 后继续写。 |
| 更新保护检查了错误数据库 | 在注册表探测前核对有效 Compose、运行中应用和备份容器的数据库配置。只支持可验证的标准连接形状，其他连接拒绝自动更新；日志不显示 DSN/密码。 |

独立复核额外发现并修复了关闭竞态：故障清理被正常退出取消时，旧实现提前从表中移除仍存活的 worker。
现在进程确认退出后才移除跟踪；取消时强制结束并等待，重叠关闭不会遗留未跟踪进程。

## 升级、数据与兼容性

没有新增 schema 迁移、清库或自动改写历史数据。角色创建和聊天合同不变。
升级仍需停写，备份注册库、全部角色库（含停用库）及部署配置，然后按 [维护合同](character-isolation.md) 手工升级。
多库备份恢复未在本轮实现；自定义数据库或无法证明备份目标一致时，一键更新脚本明确停止。
标准连接支持范围及源码进程的重启责任已在维护合同中说明。

如修复前曾实际处理项目对话，可能已有项目事实被提取为全局碎片，并进入日历、画像或 Dream。
本轮阻止新的错误提取，不自动猜测或删除旧派生内容；应依据可信备份核对并选择性修正。
未知归属旧原文不再参与角色模式的全局提取，但原文和已有记忆仍保留，不把 default 变成共享库。

## 验证证据

修复前先补回归并记录失败：提取脚本 6 条隔离断言失败；Dream 3 个响应路径失败；
生命周期最初 2 项失败；更新保护 5 个子场景失败；编码面板得到 default URL；重叠关闭新增测试也先失败。

| 命令/范围 | 结果与证据边界 |
| --- | --- |
| `python scripts/test_character_extraction.py` | 8 guards PASS。真实临时 PostgreSQL，实际提取/存储/提示构造，模型与向量生成模拟。覆盖自动/手动素材、过滤先于 LIMIT、最终模型请求无项目事实、旧原文保留及旧模式兼容。 |
| `python scripts/test_character_postgres.py` | 61 guards PASS。真实数据库、角色进程、自有 MCP；模型模拟。包括原 A/B 隔离验收、重复初始化、新增真实注册连接终止后停止全部 worker/关闭连接池，以及新 supervisor 重获锁且保留注册记录。 |
| `python scripts/test_character_scope_regressions.py` | exit 0。真实数据库的同步归属、ZIP 恢复与回滚、并发绑定、项目日历工具及画像提示回归通过。 |
| `python scripts/test_kiwi_safety_sync.py` | 178 total permanent behavior guards PASS；真实临时 PostgreSQL，模型边界模拟。 |
| `python scripts/test_character_lifecycle.py` | 8 tests OK。真实本机 HTTP 长连接、真实 Uvicorn 子进程退出；数据库故障和 worker 等待模拟。覆盖池耗尽、启动探测、临时错误重试、失锁、意外心跳退出、并发启动和重叠清理。 |
| `python scripts/test_character_dream_scope.py` | 4 tests OK，含参数化路径和断连场景。执行实际响应路径，模型及 Dream 启动器模拟。 |
| `python scripts/test_character_isolation.py` / `test_character_tool_scope.py` | 22 tests / 3 tests OK；模拟 worker、工具边界。 |
| `node scripts/test_character_panel.mjs` | 正常 URL、4 组编码 URL、19 类异常 URL、实际请求/下载/SSE、向导及标题身份守卫通过；模拟 DOM，不是浏览器人工验收。 |
| `python scripts/test_character_update_guard.py` | 7 tests OK；真实 Bash，Docker 模拟。核对配置与运行态异库、异主机、连接选项、缺值及备份目标变化，输出不含测试凭据。 |
| `python scripts/test_kiwi_prep_01.py` | 15 tests，OK（skipped=1），exit 0，207.032 秒。默认更新、续跑、多角色及自定义数据库阻断通过；Docker 模拟。 |
| 原有 11 个 Python 回归脚本 | 全部 exit 0；清单见下文。 |
| 原面板导航/日历日期 Node 脚本，框架兼容脚本 | 导航 10 guards、日历日期守卫、两次 ASGI 生命周期、静态资源、multipart、CORS 均通过。 |
| `python scripts/character_isolation_knives.py` | 6 个已有变异全部 KILLED，正式源码未被变异。另在临时副本移除面板解码、更新目标核对，新增回归均能捕获。 |
| `python -m compileall -q .` / `git diff --check` / Bash `-n` | 通过。 |

原有 11 个脚本：`test_drawer_stability.py`、`test_gateway_tool_streaming.py`、`test_stream_capture.py`、
`test_calendar_summary_generation.py`、`test_calendar_json_parser.py`、`test_mcp_recall.py`、
`test_compression_reasoning.py`、`test_calendar_delete_atomicity.py`、`test_mcp_calendar_sections.py`、
`test_calendar_period_guards.py`、`test_admin_panel_cache.py`。

真库验证均只创建随机测试库，在 finally 删除并以 SQL 确认清理；临时 PostgreSQL 最后停止。
新增脚本已加入 CI 配置，但未推送，因此没有 GitHub CI 运行结果。
原文过滤和 Dream 改动经过独立只读复核；进程清理的独立复核发现了上述竞态并补回归修复。

## 未执行与边界

- 真实模型供应商、真实 Docker 构建/Compose 重启、全浏览器人工验收、生产容量测试。
- 完整多库备份恢复、官方旧版镜像升级/回退、真实供应商调用及本轮依赖安全扫描。
- PREP 跳过 Linux 最小 PATH 矩阵；jq/helper-free、可选其他 awk 和真实 Compose 配置矩阵未执行。
- 本轮没有给 float 接线，没有新增跨应用事件协议，没有用户级鉴权。

结论：本轮六项审查问题及补充关闭竞态完成本地修复和上述阶段验证，不作为发布或生产迁移授权。
