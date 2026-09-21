# 多角色隔离审查修复记录

日期：2026-09-17。此记录是本地阶段验证，不是版本发布验收。

- 上游基线：`b01a0c506f4f10f90f30d408c0291f16720f0718`，版本 1.7.0。
- 修复前提交：`b567aea6d38bc80cdf90c991b43de8aa5bc22952`。
- 分支：`codex/character-isolation`。修复代码与本记录一起提交；没有推送、部署或修改 float。
- 本次修复是在上一轮只读审查列出的七项问题基础上实施，保留独立角色数据库/进程方案。

## 七项处理结果

| 审查问题 | 修改及合同 |
| --- | --- |
| 非标准 JSON 媒体类型忽略角色 | 共用入口先解析请求，再选择角色。支持大小写不敏感的 application/json、application/*+json、缺省 Content-Type 的 JSON；其他非空请求体返回 415。ZIP 导入和文件提取保留 multipart。 |
| 向导复制 default 地址 | 显示和复制都使用当前角色 API 前缀；向导完成键包含角色面板地址。 |
| 项目工具写全局日历/画像 | 项目上下文禁止全局日历、评论、整理/Dream 启停和提醒写工具；首次工具列表、动态展开和执行端共同守卫。角色内全局读取与项目记忆保存仍可用。 |
| 同步绕过会话项目绑定 | 聊天、同步创建、PATCH、PUT、JSON 导入和备份恢复共用事务归属检查。校验项目存活、永久绑定、现有元数据及已知账本；并发首次绑定只有一个范围成功。 |
| 更新脚本遗漏角色库 | 采用阻止不完整备份的方案。实际更新/续跑前检查有效配置、运行中的角色开关和注册表；包括停用角色在内的非 default 库存在时停止。force/no-backup/auto 不绕过；检查失败也停止。**未实现自动多库备份。** |
| 管理入口忽略冲突角色 | 创建、查询、改名和停用入口校验路径/正文/请求头，冲突返回 409，副作用前拒绝。 |
| CORS 隐藏会话 ID | 同时暴露 X-Kiwi-Character 与 X-Kiwi-Session-Id。 |

## 数据与兼容性

不清库、不复制角色数据、不自动纠正历史归属。原有表和记忆行保留；已有角色绑定表继续使用。
角色模式现在在首次同步写入时也固定项目，不能通过移动会话修改所属项目；如需另一个范围，请新建会话 ID。
普通改名仍可用。项目字段非法格式返回 400，未知项目及归属冲突返回 409。
JSON 批量导入在 rejected_details 报受控错误码；ZIP 的单个对话恢复失败计入 failed_conversations 和 scope_errors，失败事务保留原消息及墓碑。

如果使用过修复前代码并产生了绑定/元数据冲突，启动检查会拒绝继续运行，且不自动改写旧行。
应停写、备份各角色库，依据可信备份恢复一致状态；不要删除绑定记录来绕过检查。
旧单角色模式保留原同步行为，再次启用角色模式前应检查一致性。

完整升级/备份/回退流程见 [角色维护合同](character-isolation.md)。本次一键更新保护不替代多库灾备：
手工备份必须包含注册库、所有角色库（包括停用库）、映射和部署配置；跨库一致备份前停止 supervisor。
角色 ID 仍不是鉴权凭证，可信客户端可明确选择角色。

## 本地验证

环境：Windows、临时 Python 3.12 虚拟环境、Git Bash、临时 PostgreSQL 16.15。
没有调用真实模型服务，没有使用生产数据。

| 命令 | 结果与证据边界 |
| --- | --- |
| `python scripts/test_character_isolation.py` | 22 tests，OK。ASGI + 模拟 worker，覆盖 MIME、无角色回退、管理冲突、CORS、上传兼容等。 |
| `node scripts/test_character_panel.mjs` | 原 5 条 URL 守卫通过；新增执行真实向导 renderStep3 的显示/复制断言及 A/B 完成键隔离通过。模拟 DOM，不是浏览器验收。 |
| `python scripts/test_character_tool_scope.py` | 3 tests，OK。模拟工具实现，验证全局写工具执行拒绝、动态展开过滤及全局/旧模式兼容。 |
| `python scripts/test_character_update_guard.py` | 5 tests，OK。真实 Bash 脚本 + 模拟 Docker，覆盖配置、运行状态、保留角色库与无法验证范围。 |
| `python scripts/character_isolation_knives.py` | 6 个变异全部 KILLED；只在临时源码副本修改，正式实现未被变异。 |
| `python scripts/test_character_scope_regressions.py` | 通过。真实临时 PostgreSQL、实际 API/同步/工具/画像和提示构造函数，画像模型模拟。覆盖 PATCH/PUT/JSON 导入拒绝、ZIP 成功恢复与冲突失败、并发绑定、重复初始化和存量冲突拒绝启动。 |
| `python scripts/test_character_postgres.py` | 58 guards 通过。真实角色 worker、数据库与自有 MCP；模型及 embedding 模拟。 |
| `python scripts/test_kiwi_safety_sync.py` | 178 total permanent behavior guards 通过，两轮修复验证均通过；真实临时数据库，模型边界模拟。 |
| 既有 11 个 Python 回归脚本 | 全部 exit 0；脚本清单与各自边界见 [原验证记录](character-isolation-verification.md)。 |
| `node scripts/test_admin_panel_nav.mjs` / `test_calendar_period_defaults.mjs` | 原 10 条导航守卫和日历日期守卫通过。 |
| `python scripts/test_prep_framework_compat.py` | exit 0，两次 ASGI 生命周期、静态资源、multipart 与 CORS 验证。 |
| `python scripts/test_kiwi_prep_01.py` | 14 tests，OK（13 通过、1 跳过 Linux 最小 PATH 矩阵），exit 0。新增角色更新拦截测试通过；更新保护与续跑专项 2 tests 也通过。真实 Docker 未使用。 |
| `python -m compileall -q .` / `git diff --check` / Bash `-n` | 编译、空白检查与两个更新脚本语法检查通过。 |

修复前先记录了入口测试 3 个精确失败（角色字段未剥离、冲突请求返回 200、CORS 缺头）及向导复制默认地址失败。
项目工具和同步漏洞已有上一轮真实 PostgreSQL 复现；本次永久回归证明对应路径被拒绝，且全局正常功能仍可用。

新增真库脚本输出包括：

```text
PASS: PATCH cannot expose project history globally
PASS: JSON import refuses scope change; ZIP restore retains project and reports rejected scope
PASS: PUT/import/restore reject changed ownership; concurrent first binding and repeated migration safe
PASS: project diary blocked; global diary still works
PASS: project diary absent from profile input and global prompt
PASS: disposable PostgreSQL database removed
```

所有真库验证只创建随机测试库，在 finally 清理，并通过 SQL 验证不存在；临时 PostgreSQL 最后停止。

## 尚未验证

- 真实 Docker 构建、Compose 启动与实际多库备份恢复；更新测试里的 Docker 是模拟命令。
- 官方旧版本镜像升级与降级演练；同版重复初始化和 ZIP 对话恢复不能替代该验收。
- 真实模型服务、完整浏览器操作、生产容量和全部故障排列。
- GitHub CI 未运行；已登记新增 CI 入口。Linux 的 jq/最小 PATH 与可选 awk 矩阵本机未执行。
- 本轮修复进行了自查及回归，不把自查称为新的独立审阅。

结论：七项问题按上述策略完成本地修复，不作发布或生产迁移授权结论。float 仍未接入。
