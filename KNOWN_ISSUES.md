# 已知问题登记（Known Issues）

2026-09-19 本地 `codex/float-memory-api` 已补充跨应用事件协议，见 [适配说明](docs/float-memory-adapter.md)；下面角色隔离快照的“待办”描述针对旧标签。新增 API 尚未做生产/真实模型验收。

2026-09-21：`9173795` 已保护群事件不被 Dream/软化改写，并纳入 float 收据活动时间。群派生记忆来源模型、已经在旧版中消失的群条目恢复、完整收据备份恢复仍未实现或未验收；不能将本地真库回归等同于生产接受。

## 角色隔离二改开发快照（v1.7.0-luoluo.1）

版本与上游对应关系见 [二改版本维护说明](docs/fork-maintenance.md)；尚未通过正式发布验收。

- 角色模式通过独立数据库/进程隔离；不是用户级鉴权，也尚未完成生产容量与部署验证。
- W2-05b 五个记忆工具的项目范围已在**角色模式**补齐；旧单角色模式保持原实现，下文历史登记仍适用于该模式。
- 部分流式分支使用网关工具列表，尚未完整合并客户端传入 `tools`；float 工具兼容性待独立处理。
- 角色模式的外部 MCP 暂禁用；跨应用事件协议、永久擦除角色、横向扩容仍待办。
- 2026-09-17 审查的七项问题已补入口、会话事务、工具及更新保护；项目全局写工具禁用，完整多库自动备份仍未实现，更新脚本会停止并要求手工备份升级。见 [修复验证](docs/character-isolation-review-fixes.md)。
- 第二轮补齐全局提取原文过滤、编码面板地址、Dream 标记范围、独立心跳连接池、注册表故障退出及实际备份目标核对。见 [第二轮修复验证](docs/character-isolation-review-2-fixes.md)。修复前可能已产生的错误全局派生内容不会自动删除，应依据备份人工核对。
- 项目对话仍不自动提取碎片；没有新增项目日历/Dream。详见 [完整合同](docs/character-isolation.md)。

本文件登记代码审计发现、但**当前批次有意未修**的项：要么是设计取舍、要么是低危技术债、
要么需要产品决策。修过的高危项见 git 历史，不在此列。

> 行号为审计时的近似位置，经多次改动后可能有偏移，以函数名为准。

---

## 一、设计取舍（不视为 bug）

- **纯 `.env` 部署只支持 OpenAI 格式。** Anthropic 原生必须经管理面板配置供应商
  （README 已说明）。因此以下「硬编码 OpenAI / Bearer」是符合该约束的，不修：
  - `/v1/models` 的环境变量兜底分支（`main.py` `list_models`）用 `Authorization: Bearer`。
  - 切窗摘要压缩的异常兜底（`main.py` 约 3348）固定 `use_api_format="openai"`。
  - `resolve_model_endpoint` 的环境变量兜底（`database.py` 约 3691）。
- **`API_BASE_URL` 默认 OpenRouter** 是零配置默认值，刻意保留。

---

## 二、中危：待产品决策 / 需谨慎处理（暂缓）

> ✅ 工具抽屉 auto 钉选语义分叉、并发快照不完整 —— 两项已在后续小 PR 修复：
> auto 统一为纯语义路由（不读 `mcp_manual_ids`，与 `handle_meta_tool` 一致）、
> 锁内一并快照 `_category_embeddings`/`CATEGORIES`/`TOOL_SCHEMAS`/`_external_categories`，
> 并补了 `tests/test_drawer.py` 行为测试。不再列为待办。

- **矛盾检测漏「纯数字/单字事实更新」**（`database.py` `detect_contradictions`）。
  字符重叠兜底对「我女儿五岁 → 六岁」这类整句几乎相同、只改一个字的更新，会算成
  近重复（>0.85）而漏判。`tests/test_logic.py` 有 INFO 标注当前行为。
  → 根治需语义/数值感知，非字符重叠能解决，留待后续。

---

## 三、低危技术债（登记备查）

> ✅ 已在「顺手清低危」批次修复（不再列为待办）：
> - Dream 软化参数改读已有配置 `auto_soften_*`（不再写死 5/15、不漏 cooldown）
> - `update_user_profile` 回退链补 `default_digest_model`
> - `/admin/credits` 环境兜底改用通用查询 `_query_generic_credits`（不再只认 OpenRouter）
> - OpenAI URL 拼接前归一化后缀（误填 `.../messages`、`.../chat/completions` 不再拼错）
> - 本地搜索解析到 0 条但页面非空时打告警日志（区分「解析失败」与「真无结果」）

### 工具 / MCP / 搜索
- **W2-05b：聊天抽屉的五个记忆工具尚未接项目 scope。** 当前 W2-05 已保证已删或未验证项目进入隔离态，`memory` / `conversation` 两类工具不会暴露；但 global / live project 内部的搜索、保存、最近记忆与锁定/解锁仍沿用旧 MCP 执行器。项目内保存可能落成全局、全局 recent 可能列出项目项。W2-05b 必须在 W2-06a 前关闭这笔债。
- **W2-05 历史与读取合同。** 历史账本行不回填；未知归属行不进新读路径；未带项目默认全局。全局记忆、日历与 Dream 是共享底座，项目私有层单向封闭。scope 每轮只生成一份快照：metadata 与 payload 不一致时按 metadata 读取并记 `scope_mismatch`；已删或未验证项目进入隔离态，只给共享底座。新项目尚未同步完成的首轮会记 `scope_unverified`。
- 外部 MCP **不支持鉴权**：`_normalize_external_servers` 丢弃 `auth`/`headers`，client 也不透传；
  需要 Bearer 的外部 MCP 会 401、且失败被吞成「该 server 无工具」。
  → 属「加能力」而非小修（要给 transport client 透传 header），单独评估，暂留。
- 自家 MCP 靠 URL 子串 `"/memory/mcp"`/`"/calendar/mcp"` 识别（`tool_drawer.py`），改挂载路径会失效。
  → 现行挂载下有效，硬化收益有限，暂留。
- 本地搜索引擎正则匹配结果页 class（`web_search.py`），页面改版会解析到 0 条。
  → **已加告警日志**区分「解析失败」与「真无结果」；但正则本身仍随页面改版失效，根治需换解析方式。
- `record_tool_use` 在 session 被 LRU 淘汰后静默 no-op（`tool_drawer.py`）；影响极小，暂留。

### 认知后台（Dream / 整理 / 提取）
- Dream 自动触发 `should_dream` 的 `5/7/3` 阈值硬编码（`dream.py` ~784），与可配的
  `dream_drowsy_threshold`（默认 30）两套标准不一致。
  → 统一需新增配置项 + schema（属功能改动，非「顺手」），留待后续。
- 后台任务兜底默认模型名硬编码 `anthropic/claude-haiku-4`（OpenRouter 命名风格），非 OpenRouter
  供应商上该 model_id 可能 404。→ 改默认值会影响零配置开箱体验，暂留。

## KIWI-PREP-01

1.7.0 暂留 mcp 1.12.4 的 CVE-2025-66416 / CVE-2026-52869 / CVE-2026-59950，限时例外由 BUILD-01 随 2.0.0 解除；其他扫描结果不豁免。详见 [升级预告](docs/UPGRADING.md)。

预检 fail-open：尚未观察到远程使用、状态口不可达或升级门无法解析时仅提示；登记值存在不证明正确。Zeabur Auto Deploy 和跳过准备版的用户不受预检覆盖。Quick Tunnel 官方不支持 SSE。
