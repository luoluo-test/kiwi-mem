# 多角色隔离：本地阶段验证记录

本记录是开发阶段证据，**不是版本发布验收报告**。未指定新版本，不创建 `docs/acceptance/vX.Y.Z.md`。

- 日期：2026-09-16 至 2026-09-17（Asia/Shanghai）。
- 上游基线：`b01a0c506f4f10f90f30d408c0291f16720f0718`，上游版本 1.7.0。
- 实现提交：`d846daf27e3e0898c843d57e5a1a16458a44f01e`。
- 分支：`codex/character-isolation`。本记录提交只追加验证证据，不改变实现。
- 环境：Windows、本地临时 Python 3.12 虚拟环境、PostgreSQL **16.15** 便携测试集群。
  二进制来源：[EDB 官方 PostgreSQL binaries](https://www.enterprisedb.com/download-postgresql-binaries)。
- 没有访问生产数据库，没有真实模型服务调用，没有远程推送、CI 运行或部署。
- 测试代码、CI 后续入口、接口/迁移合同都已随实现版本化。

## 验证结果

| 命令/项目 | 精确结果 | 证据类别 |
| --- | --- | --- |
| `python -m compileall -q .` | exit 0，无编译错误 | 本地语法 |
| `git diff --check`、`git diff --cached --check` | exit 0；仅 Git 的 LF/CRLF 提示，无空白错误 | 本地差异 |
| `python scripts/test_character_isolation.py` | **16 tests，OK，exit 0** | 模拟注册表/worker 的 ASGI 与入口单元测试 |
| `node scripts/test_character_panel.mjs` | **5 guards，exit 0** | URL 绑定计算，非浏览器视觉验收 |
| `python scripts/character_isolation_knives.py` | **3 个变异全部 KILLED，exit 0** | 临时源码副本负向变异 |
| `KIWI_TEST_DATABASE_URL=<本机临时集群> python scripts/test_character_postgres.py` | **58 guards，exit 0** | 真实 PostgreSQL、真实独立 worker、真实自有 MCP；模型/embedding 为 HTTP 模拟服务 |
| `KIWI_TEST_DATABASE_URL=<本机临时集群> python scripts/test_kiwi_safety_sync.py` | **178 total permanent behavior guards，exit 0**；前后两次通过 | 既有真库守卫；模型/HTTP 边界模拟 |
| 下表 11 个既有 Python 回归脚本 | **11/11 exit 0** | 原有测试各自模拟边界 |
| `node scripts/test_admin_panel_nav.mjs` | **10 guards，exit 0** | 管理面板行为守卫 |
| `node scripts/test_calendar_period_defaults.mjs` | `PASS: admin calendar defaults use previous complete periods`，exit 0 | 管理面板日期守卫 |
| `python scripts/test_prep_framework_compat.py` | `PASS: two fresh application lifespans, static admin, multipart upload, CORS`，exit 0 | 两次真实 ASGI 生命周期，非 Docker |
| `python scripts/test_kiwi_prep_01.py ApplicationGuards DeliveryGuards` | **6 tests，OK，exit 0** | 可在 Windows 运行的 PREP 应用/交付子集 |
| `python scripts/test_kiwi_prep_01.py` 全套 | exit 1，**13 tests，errors=22（含子测试），skipped=1** | **BLOCKED**：更新脚本需要的 POSIX shell/工具缺失；不能称为全绿 |

既有 Python 回归的逐项结果：

| 脚本 | exit |
| --- | --- |
| `scripts/test_drawer_stability.py` | 0 |
| `scripts/test_gateway_tool_streaming.py` | 0 |
| `scripts/test_stream_capture.py` | 0 |
| `scripts/test_calendar_summary_generation.py` | 0 |
| `scripts/test_calendar_json_parser.py` | 0 |
| `scripts/test_mcp_recall.py` | 0 |
| `scripts/test_compression_reasoning.py` | 0 |
| `scripts/test_calendar_delete_atomicity.py` | 0 |
| `scripts/test_mcp_calendar_sections.py` | 0 |
| `scripts/test_calendar_period_guards.py` | 0 |
| `scripts/test_admin_panel_cache.py` | 0 |

## 新增真库测试实际做了什么

测试创建随机 `acceptance_character_*` 注册库，再由正式创建角色接口建立 A/B 库及私有进程。
只监听本机端口。上游模型与 embedding 由测试 HTTP 服务返回可预测数据，实际请求体在内存中捕获。
没有把模型回答当作“提示词未泄漏”的判据，隔离断言直接核对网关发出的 prompt。

已执行的关键行为：

- default 中已有记忆和画像可以使用；重复注册迁移后记忆逐列不变；A/B 从空记忆库开始。
- 相同角色显示名不混淆身份；改名保留 ID。
- A/B 并发使用同一个会话 ID，提示词仅出现各自独有事实；A 换会话仍能召回。
- 两角色后台自动提取分别落库；重复 A 事实不重复插入。
- 辅助生成不落对话、不注入记忆、不提供工具；非流式/流式模型返回 Dream 标记也不能启动 Dream。
- 角色与项目双重约束生效；B 使用只存在于 A 的项目被拒；同角色会话改项目被拒。
- 真实自有 Memory MCP 的 recent 只读对应角色全局记忆，排除另一角色和项目私有记忆。
- B 的关键词/向量检索结果不含 A 的事实。
- 通过同步会话接口提供日历原文，实际生成 A/B 日页面、更新画像、运行 Dream；模型请求素材与产出保持分离。
- 日历/画像/Dream 的全局素材不含项目私有事实；A Dream 后 B 记忆和锁定字段保持不变。
- 真实自动提取触发矛盾检测：A 旧事实失效；B 同数字 ID 事实仍有效，B 无新增关联边。
- ZIP 记录角色及项目归属，A 备份导入 B 在写入前被拒绝。
- 删除 A 的同 ID 记忆/同 ID 会话不影响 B；停用 A 停止进程且后续访问返回 410。
- supervisor 生命周期重启后仅恢复 active 角色，B 数据仍在，A 停用墓碑仍生效。

58 条守卫的完整脱敏尾行见 [character-isolation-evidence.txt](character-isolation-evidence.txt)。

## 负向变异与调试记录

最初的新合同测试在旧实现上因不存在角色模块报 `ModuleNotFoundError: character_gateway`（exit 1）。
后续真正的行为负向证据来自临时副本中的三个变异：

1. 把所有角色选择强行改为 default：路由/并发隔离断言失败。
2. 去掉 worker token 检查：私有端点拒绝断言失败。
3. 去掉固定 worker 角色头检查：角色冲突拒绝断言失败。

三个变异都只改临时副本；正式工作区未被变异污染。最终重新运行 16 项入口测试为 OK。

开发中发现并修复了辅助请求禁用工具误伤旧 `skip_system_prompt` 测试的问题；
新行为限定为显式 `memory_mode: auxiliary`，既有流式工具守卫恢复通过。
新增真库测试也修正了测试夹具的查询回显判断、受控向量相似度、日历 HTTP 方法和原文来源；
未把这些中间失败算入最终 PASS。

## 清理证据

两套真库脚本均在 `finally` 删除自己的随机数据库。角色测试删除注册库及 A/B 库后执行：

```sql
SELECT count(*) FROM pg_database WHERE datname = ANY($1::text[]);
-- 参数仅为本轮自己创建的数据库名；结果：0
```

尾行：`PASS: SQL proves disposable database cleanup`。
既有守卫尾行：`Removed disposable PostgreSQL database: kiwi_safety_<随机串>`。
最后用 `pg_ctl ... -m fast -w stop` 关闭本地临时集群，返回 `server stopped`。
没有删除用户数据目录，也没有安装系统 PostgreSQL 服务。临时便携二进制、虚拟环境及测试日志保留在系统临时目录，未进入仓库。

## 未执行或不能替代的证据

- **GitHub CI 未运行**：只增加了后续 CI 入口，没有远程操作，不能提供 CI run 链接。
- **真实模型服务未验证**：未使用真实 API Key；测试 embedding 和模型响应均为模拟数据。
- **Docker 构建/Compose 实际启动 BLOCKED**：本机没有 Docker。源码进程测试不等于容器部署验证。
- **PREP 完整 Linux 更新脚本 BLOCKED**：Windows 缺少其需要的 shell、jq/awk 等矩阵和 Compose；上表已如实保留失败退出码。
- **未做完整浏览器视觉验收**：检查了静态资源/ASGI、URL 绑定和原有面板守卫；没有逐页人工点击验收。
- **未做整套官方旧版本镜像升级、备份恢复与降级演练**：仅证明注册迁移可重复、存量行不变以及同版重启恢复。
- **未做生产容量、所有异常断连/强杀排列与全部 Dream action/日历周期端到端组合**。
  日历周期仍有原有模拟守卫；新增真库 Dream 使用模拟叙述响应，不冒充所有场景生成动作都已验收。
- **没有独立审阅者的 diff review**：已自查真实差异，但不把自查写成独立审查。

结论：本地隔离实现和上述阶段验证完成；**不作“允许发布/可直接迁移生产”的结论**。
真实模型、Docker/完整迁移与回退、浏览器验收及独立审查仍应在后续发布准备中完成。
