# Changelog

## 1.7.0-luoluo.1 — 二改开发快照 (2026-09-18)

- 二改仓库：<https://github.com/luoluo-test/kiwi-mem>；标签：`v1.7.0-luoluo.1`。
- 上游基线：[LucieEveille/kiwi-mem v1.7.0](https://github.com/LucieEveille/kiwi-mem/tree/v1.7.0)，提交 `b01a0c506f4f10f90f30d408c0291f16720f0718`。
- 新增可选多角色隔离：每角色独立数据库和固定身份工作进程，覆盖聊天、记忆、工具、画像、日历、Dream、管理入口和并发状态。
- 包含两轮审查修复的会话/项目归属守卫、辅助请求限制、后台生命周期和升级保护；既有数据保留在独立默认角色。
- 统一运行时版本信息，记录上游准确基线及后续版本编号规则，见 [二改版本维护说明](docs/fork-maintenance.md)。
- 这是代码追踪快照，未完成正式发布验收；float 尚未接入，未部署或迁移生产数据。详细限制和分阶段验证见 [角色隔离维护文档](docs/character-isolation.md)。

以下保留上游变更记录。

## 1.7.0 — Unreleased (2026-09)

- 修补 FastAPI / Starlette 依赖，保留 MCP、httpx、uvicorn 基线。
- MCP 登记预告、无地址值的观察数据和只读状态口；不改变访问行为。
- 更新脚本增加配置预检、带状态续跑与有界 MCP initialize 健康探针。
- 修复升级时覆写操作员 `PORT` 的回归；统一 Python / jq / 无助手的端口取值与引号、BOM 处理，探针内部使用 `LISTEN_PORT`。
- Starlette 静态资源支持 Range（206 / 416 / `Accept-Ranges`）；属于 HTTP 行为变化，访问控制保持原样。The dependency upgrade enables static-file Range responses without introducing access controls.
- 部署配置透传与 [升级预告](docs/UPGRADING.md)。

限时风险例外：1.7.0 暂留 `mcp==1.12.4`，其三条公告 CVE-2025-66416 / CVE-2026-52869 / CVE-2026-59950 仍在。理由：升 mcp ≥ 1.23 会让 SDK 对本机 host 自动开启 Host / Origin 保护、远程 MCP 在准备版就被拒，违背“先提醒再改规则”。解除条件：KIWI-BUILD-01 合入 `release/kiwi-sync` 并随 2.0.0 发布。负责票：BUILD-01。本例外不豁免其他任何扫描结果。
