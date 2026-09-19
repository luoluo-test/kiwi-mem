# Kiwi-Mem Luoluo 二改版本维护

## 2026-09-19 本地 float 适配增量

在 `51ed818a03ae9e85e4c9891bc3c5f1f1c852e425` 上接续中断任务，独立分支 `codex/float-memory-api` 新增 `float-memory-v1` 事件/召回合同及角色库收据表。配套 float 分支为 `codex/kiwi-memory-adapter`；真实 HTTP/PostgreSQL 联调通过，供应商为替身。详细字段、迁移/回退、覆盖与限制见 [float 适配说明](float-memory-adapter.md)。本轮未创建新版本标签、推送、发布、部署或修改生产数据，以下旧快照记录保留。

## 当前版本与上游对应关系

本仓库名称为 **Kiwi-Mem Luoluo 多角色隔离版**，服务仍使用 `Kiwi-Mem` 名称。
当前二改版本为 **`v1.7.0-luoluo.1`**，用于固定已完成两轮审查修复的开发快照。

| 项目 | 对应值 |
| --- | --- |
| 二改仓库 | [luoluo-test/kiwi-mem](https://github.com/luoluo-test/kiwi-mem) |
| 二改版本 / Git 标签 | [`v1.7.0-luoluo.1`](https://github.com/luoluo-test/kiwi-mem/tree/v1.7.0-luoluo.1) |
| 功能分支 | `codex/character-isolation` |
| 审查入口 | [Draft PR #1](https://github.com/luoluo-test/kiwi-mem/pull/1) |
| 原作者仓库 | [LucieEveille/kiwi-mem](https://github.com/LucieEveille/kiwi-mem) |
| 对应原版 | [`v1.7.0`](https://github.com/LucieEveille/kiwi-mem/tree/v1.7.0) |
| 上游准确提交 | [`b01a0c506f4f10f90f30d408c0291f16720f0718`](https://github.com/LucieEveille/kiwi-mem/commit/b01a0c506f4f10f90f30d408c0291f16720f0718) |
| 基线核实日期 | 2026-09-18 |

上游 `v1.7.0` 是附注标签；这里记录的是解引用后的代码提交，不是标签对象 ID。
当前 `main` 保留上游基线，二改内容在上述功能分支及二改标签中；查看或下载本快照请选择二改标签。
本次只建立版本身份和追踪标签，不创建 GitHub Release、不合并 PR、不部署，也不迁移生产数据。

## 编号规则

采用 **`v<上游基线版本>-luoluo.<二改序号>`**。

- `v1.7.0-luoluo.1`：基于上游 `v1.7.0` 的第一个已编号二改快照。
- 同一上游基线下继续修改，二改序号递增，例如 `v1.7.0-luoluo.2`；旧标签不移动、不覆盖。
- 实际合并并验证一个新的上游版本后，更新基线版本与准确提交，二改序号从 `1` 开始。
  例如完成上游 `v1.8.0` 的同步后才可使用 `v1.8.0-luoluo.1`；这只是规则示例，不代表已同步该版本。
- 仅挑选上游个别修复时，保留当前基线，单独记录挑选的提交；不能因此声称已完整同步新的上游版本。
- 不创建或覆盖无后缀的上游标签（如 `v1.7.0`），以免混淆原版与二改版。

`-luoluo.1` 在 SemVer 中属于预发布后缀，标准排序低于无后缀的 `1.7.0`。
这里用它标识二改开发快照；判断上游对应关系须使用独立的基线字段，不能只比较版本大小。
现有更新脚本按 Git 分支和提交判断更新，版本号不会自动改变其更新来源。

## 版本信息的维护位置

[`kiwi_version.py`](../kiwi_version.py) 是运行时版本和上游基线的唯一来源：

- `VERSION`：不带前导 `v` 的二改版本。
- `UPSTREAM_VERSION`、`UPSTREAM_TAG`、`UPSTREAM_COMMIT`、`UPSTREAM_REPOSITORY`：原版版本、标签、准确提交及仓库。
- `FORK_REPOSITORY`：二改仓库。

主服务及角色监督服务的 OpenAPI 版本、管理面板和 MCP 访问状态读取同一版本值。
角色工作进程或旧单角色服务的 `GET /` 保留原有 `version: "Kiwi-Mem v…"` 格式，
另提供 `fork_version`、`upstream_version`、`upstream_tag`、`upstream_commit`、`upstream_repository`。
角色模式下使用 `/characters/{character_id}/` 查询所选角色的该状态；未携带角色的根路径仍进入 `default`，角色管理入口为 `/character-manager`。
这些是软件版本字段，角色身份、数据库 schema、备份格式和 MCP 协议版本不随之改名。

建立后续版本时，一并更新运行时常量、README / README_EN、CHANGELOG 及下表，
完成适用检查后提交，再给该提交建立同名附注标签。附注中保留上游标签与准确提交。
使用 `git rev-parse 'v1.7.0-luoluo.1^{commit}'` 可得到本快照的准确二改提交；不要把会变化的分支 HEAD 当作永久版本记录。

## 版本对应记录

| 二改标签 | 上游标签 | 上游提交 | 内容与验证范围 |
| --- | --- | --- | --- |
| `v1.7.0-luoluo.1` | `v1.7.0` | `b01a0c506f4f10f90f30d408c0291f16720f0718` | 多角色隔离与两轮审查修复，附加统一版本信息；开发快照 |

版本标签固定代码状态；此前的 [本地验证记录](character-isolation-verification.md)、
[第一轮修复记录](character-isolation-review-fixes.md) 和 [第二轮修复记录](character-isolation-review-2-fixes.md)
保留各自的测试范围和时间，不将旧结果当作新提交的正式发布验收。
正式发布仍须按 [发布验收母文档](Release%20Acceptance.md) 指定目标版本与提交、完成验收并取得发布授权。

## 后续对齐上游

保留 `origin` 指向二改仓库、`upstream` 指向原作者仓库，以及完整上游提交历史。
同步前确认工作区状态、备份数据库及配置，再读取维护要求，在独立 `codex/` 分支上处理差异。
抓取上游后核实待同步标签实际指向的提交，审查合并冲突并运行与改动相关的回归及数据库检查。
完成验证后更新上述对应记录，通过 Draft PR 审查；合并、正式发布、部署和生产迁移分别安排。

多角色模式下不要直接运行会覆盖数据或忽略角色数据库的一键升级流程。
功能设计、迁移与回退条件、接口合同以及 float 接入缺口继续以
[多角色隔离维护文档](character-isolation.md) 为准。float 尚未接入，此版本号不表示已完成互通。
