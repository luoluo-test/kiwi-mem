# 多角色隔离维护与接口合同

状态：二改开发快照 **`v1.7.0-luoluo.1`**，基于上游 `v1.7.0`；未通过正式发布验收。
准确基线、版本对应关系与后续同步规则见 [二改版本维护说明](fork-maintenance.md)。

## 来源与维护

- 上游：<https://github.com/LucieEveille/kiwi-mem>
- 基线：`b01a0c506f4f10f90f30d408c0291f16720f0718`（2026-09-16 读取 remote main；2026-09-18 核实为上游 `v1.7.0` 标签实际指向的提交）。
- 初始目录没有 `.git`；抓取上游历史后，以 mixed reset 建立索引而不覆盖文件。
  `git status --short` 为空，确认下载包全部受跟踪文件与该提交一致。
- 二改仓库：<https://github.com/luoluo-test/kiwi-mem>；功能分支：`codex/character-isolation`。
  已上传功能分支并建立 [Draft PR #1](https://github.com/luoluo-test/kiwi-mem/pull/1)；未合并、部署或迁移生产数据。
- float 只读取了维护记录和 `lib/memory-storage.ts`；未修改，也未完成双方互通。

优先保留上游实现：新增 `character_gateway.py` 管注册表、进程及转发；
`character_worker.py` 是私有进程入口；`character_boundary.py` 管入口和会话归属；
`character_tools.py` 修补角色模式的记忆工具项目范围。
上游更新时重点审查 `main.py` 生命周期、工具执行入口、管理面板 API 地址、MCP 回环地址和后台任务。
不可把角色子进程改成共用 `main` 模块的多个 ASGI 实例：模块级状态会重新共享。

## 隔离方式与信任边界

一个对外服务端口，一个 supervisor；每个 active 角色一个固定身份的工作进程、一个独立 PostgreSQL 数据库。
这不是每条表记录追加 `character_id` 的方案。最外层角色作用域由数据库和进程确定，原项目作用域继续在该库内生效。

| 内容/入口 | 角色边界 |
| --- | --- |
| 对话事件、同步会话、消息、删除墓碑、重置世代、压缩摘要 | 角色独立库；会话额外绑定项目 |
| 提取、手动添加、导入、向量与关键词检索、提示词注入 | 同一工作进程只连自己的库 |
| 去重、矛盾、关联边、热度、召回计数、锁定 | 同库处理，ID 只在该角色内有意义 |
| Dream、场景、画像、日/周/月/季/年日历 | 各自的库、调度器、取消状态和锁 |
| 记忆工具、历史搜索、管理接口、导入导出、重置 | 全部经过相同的角色路由；自有 MCP 回调自己的私有端口 |
| 提取计数、工具抽屉会话、配置/工具缓存、后台任务集合 | 进程内独立；任务不能切换到其他角色 |
| 人设文件、供应商配置 | 新角色不继承旧 `system_prompt.txt` 或数据库配置；环境中的供应商连接配置仍是部署级默认值 |

默认库只归 `default`，绝不是所有角色共享的底座。角色内原来的“全局记忆 + 当前项目记忆”语义保留。
项目内容不能流向同角色其他项目/全局；新增工具守卫和日页面碎片过滤补上了两处缺口。
项目对话仍按上游合同**只落账、不自动提取项目碎片**，这轮没有变更该产品语义。
可显式向项目添加记忆，或由项目聊天内的保存工具添加。
全局自动/手动提取的对比素材、每日碎片整理、日页面的碎片大纲排除项目私有素材。
自动和手动提取的原始对话素材也只读取 `scope_known=TRUE AND project_id IS NULL`，先过滤再取最近条数；
不把项目原文或归属未知的历史原文重新提取成全局记忆。旧原文保持存储，既有记忆不重写。
角色模式的手动提取接口不接受项目参数，返回 400 `project_extraction_not_supported`。

**角色 ID 不是鉴权。** 外层 API 与原版一样没有用户级权限系统；能调用服务的可信客户端/管理员可以显式选择任何角色。
应由部署方已有的认证代理限制访问。此功能防止错误共享、模型工具越出既定角色及并发串读写，不防止有权调用入口的客户端故意冒充其他角色。
同一操作系统和 PostgreSQL 账号仍是可信计算边界，不是敌对租户沙箱。
模型没有“切换角色”的内置工具，工具参数里的角色字段被拒绝。

子进程仅监听 loopback，并要求 supervisor 生成的私有 token；不会把 token 返回客户端。
supervisor 每 5 秒续租；失联 45 秒后工作进程退出，避免孤儿调度器无限继续写入。
数据库 advisory lock 防止重复 supervisor/重复同角色调度器。异常重启可能在旧进程退出前短暂不可用，返回错误，不回退 default。
运行时 worker 崩溃由 supervisor 重启；正在执行的请求可能失败，不能承诺跨进程崩溃恰好一次执行。
心跳及启动探测使用独立连接池，不与聊天/SSE/MCP 长连接争用业务连接。
注册表临时查询异常或超时会重试，健康 worker 继续续租；注册表连接断开意味着监督锁已丢失，
服务会停止旧 worker、关闭连接池并退出。心跳任务意外结束也执行该退出流程。
Compose 的重启策略负责重新启动；源码直跑时需由进程管理器或管理员重启，不能承诺源码进程自行复活。

每个角色会占用 Python 进程、词典、连接池和调度器。默认最多 16 个 active/provisioning 角色（含 default），
可设置 `KIWI_MAX_CHARACTERS`；这不是负载容量承诺。请先在实际主机测内存和 PostgreSQL 连接上限。
只运行一个 supervisor，不用 `uvicorn --workers N`；多机横向扩展尚未实现。

## 启用与迁移

1. 停止旧网关写入，在隔离环境完成备份恢复演练。
2. 使用 `pg_dump -Fc` 备份旧数据库，并备份部署配置和人设文件；密钥文件不要提交仓库。
3. 确认 PostgreSQL 账号有建库权限（`CREATEDB` 或对应管理员权限），且允许独立数据库连接。
   受限托管数据库若不能建库，暂不支持本方案，不能降级到共用记忆表。
4. 在 `.env` 设置 `KIWI_CHARACTER_ISOLATION=true`，保持原 `DATABASE_URL`，
   启动时要求 `MEMORY_ENABLED=true`（角色内仍可用管理配置关闭提取），
   用现有 Compose 启动方式启动。源码方式可运行 `python character_gateway.py`；
   `python main.py` 也读取上述开关。未开启时保留旧单角色启动方式。
5. supervisor 在旧库只增加 `kiwi_characters` 注册表及 default 映射；工作进程新增
   `kiwi_character_sessions` 归属表，原表仍运行上游幂等初始化。**不清空、不复制、不重写旧记忆行。**
6. 新角色以随机内部库名从 `template0` 建空库，启动同版 schema 初始化。
   注册表先记录 `provisioning`，建库后变为 `active`；建库中断后重启会继续该条记录。
   不会把失败角色映射到 default。创建超时应先 GET 查询状态，不要换一个 ID 重复创建。
7. 打开 `/character-manager` 创建/查询/改名/选择角色；打开角色面板配置该角色的人设和供应商。
   不自动复制旧画像、日历、配置或私有记忆。环境变量供应商参数可为新角色提供统一连接默认值。

重复启动重复执行注册表/schema 初始化，不会重复 default 或重新建已有角色库。
升级所有角色必须使用同一代码版本。某角色初始化失败会使启动失败，先修复环境，不能绕过错误继续共享库。
升级前后查看 `/characters`，并逐角色检查数据条数、聊天、工具和任务结果。

开启多角色后，完整备份必须包含**旧库中的注册表以及所有角色库（含停用归档）**。
`update.sh` 的原单库备份不满足此要求。更新脚本现在在备份、合并与部署之前检查有效 Compose 配置、运行中服务及数据库注册表：
发现角色模式、任何非 default 角色库，或无法验证范围时停止；`--force`、`--no-backup`、自动和续跑模式不能绕过。
`--check` 仍仅查询更新。旧单角色更新要求数据库运行且 Compose 范围可验证。
更新脚本仅支持可核实的标准 `db:5432` 连接：有效 Compose 中应用 URL 必须为
`postgresql://<POSTGRES_USER>:<POSTGRES_PASSWORD>@db:5432/<POSTGRES_DB>`，且与运行中的应用及 db 容器配置一致。
此保护只接受上述三个值由字母、数字、下划线、点、连字符组成、没有额外连接查询参数的形状；
其他主机、数据库、编码凭据、连接选项或无法解析的配置均明确停止，改用手工备份升级。
这是单库更新脚本的支持范围限制，不限制服务本身使用文档支持的 PostgreSQL 连接。
本次没有实现自动多库备份；请停 supervisor，按下面的完整备份组流程手工升级，不要删除注册表来绕过保护。
内部库名可由受信管理员查询 `SELECT id,database_name,state FROM kiwi_characters` 获得，接口不公开连接信息。
每库分别 `pg_dump -Fc`，维护角色 ID—库名映射。跨库一致备份需先停 supervisor；只备份旧库会漏掉新角色。
恢复时使用完整备份组及原映射，在副本集群验证。不要把注册表恢复到仍连接旧角色库的线上环境。

回退条件：停止 supervisor/全部子进程后，先在备份副本上验证旧代码。
关闭开关可恢复旧 default 服务；新增表对旧代码是附加表。旧代码不理解注册表和多角色路由，
因此**不能继续接收新角色请求**，也不能把新角色库合并到 default。保留各库以便再次升级。
跨版本备份恢复、完整旧版本容器回退仍需独立验收，不以本次测试代替。

本轮修复不追加数据迁移、不清空或自动修正已有行。若实际使用过修复前的角色版本，
项目原文可能已被全局提取并参与后续整理；修复只能阻止新的错误提取，不能准确反推出旧派生内容的全部来源。
升级前停写并备份，按可信来源核对受影响的全局碎片及其日历/画像/Dream 派生内容，再安排有选择的修正或恢复。
不要批量删除旧库或把未知归属原文自动认定为全局来补数据。

## 接口合同

推荐统一使用路径前缀：`/characters/{character_id}/<原有路径>`。
例如 A 的面板为 `/characters/A/admin/`。同浏览器同时打开 A、B 页签，API 地址取各自 URL；不依赖共享 cookie 或 localStorage。
未加前缀的旧请求只进入 default（`/admin` 重定向到 default 面板）。

也可在原有路径上提供请求头 `X-Kiwi-Character: A`，或 JSON 顶层 `character_id: "A"` / `characterId: "A"`。
多个来源必须一致，否则 409；创建、查询、改名、停用角色接口也遵守该规则。显式 null、空串、非法类型不视作缺省。
JSON 媒体类型不区分大小写，支持 `application/json`、`application/*+json`；未给 Content-Type 的原始 JSON 也先解析角色。
其他非空请求体返回 415；仅 ZIP 导入与 `/v1/files/extract` 保留 multipart 上传，角色由路径/请求头指定。
跨域响应同时暴露 `X-Kiwi-Character` 与 `X-Kiwi-Session-Id`。角色向导生成该角色的 `/v1` 地址，完成状态按角色面板地址保存。
面板会对 URL 路径解码一次后校验角色；例如 `%41` 与 `A` 使用同一角色。
编码分隔符、非法编码、非法/缺失角色或不支持的面板路径会停止加载，不能退回 default。
面板标题、请求、下载、SSE 和向导共用这一角色身份。
角色 ID 允许 1–128 个 ASCII 字母、数字、下划线、连字符，区分大小写，不去空格、不根据姓名或模型推导。
查询字符串角色选择不受支持，会返回 400，防止误以为 `?character_id=A` 生效。
不要只把 ID 填进 `project_id`。角色字段在转发模型前剥离。

| 操作 | 请求 |
| --- | --- |
| 创建空角色 | `POST /characters`，`{"id":"A","name":"人物显示名"}`；201 |
| 列出角色（含停用记录） | `GET /characters` |
| 查询指定角色 | `GET /characters/A` |
| 改名，ID 不变 | `PATCH /characters/A`，`{"name":"新名字"}` |
| 停用角色并停止后台进程 | `DELETE /characters/A`；200，`data_retained:true` |
| 聊天 | `POST /characters/A/v1/chat/completions`，标准 OpenAI 请求体 |
| 查看/检索记忆 | `GET /characters/A/debug/memories?q=关键词&limit=20` |
| 添加记忆 | `POST /characters/A/debug/memories`，`{"content":"独有事实","title":"标题","importance":7}` |
| 添加项目记忆 | 同上，额外提供已存在的 `project_id`，错误归属拒绝 |
| 修改指定记忆 | `PUT /characters/A/debug/memories/123`，`{"content":"更新后的事实"}` |
| 删除指定记忆 | `DELETE /characters/A/debug/memories/123`；锁定条目需先解锁或显式 `force=true` |
| 项目管理 | `/characters/A/sync/projects` 及原有 CRUD 接口 |
| 会话/消息管理 | `/characters/A/sync/conversations` 及原有 CRUD 接口 |
| 历史检索 | `/characters/A/search/messages?q=关键词`；项目筛选沿用原合同 |
| MCP | `/characters/A/memory/mcp`、`/characters/A/calendar/mcp` |
| ZIP 导出/导入 | `GET /characters/A/sync/export`、`POST /characters/A/sync/import-backup`（multipart `file`） |

管理接口仍保留上游的部分 HTTP 200 + `error` 对象约定，客户端必须同时检查 HTTP 状态和响应 `error`。
管理界面可查看该角色的所有项目记忆；聊天/工具则按当前项目范围读取。
直接 MCP 记忆工具使用该角色的全局范围；本轮没有给直接 MCP 增加项目选择参数。
聊天抽屉五个记忆工具有项目快照，搜索/最近读取全局加当前项目，保存只写当前项目，锁定/解锁只修改当前项目范围。
项目聊天不暴露、也不能执行全局写工具：日历写入、评论、整理/Dream 启停、提醒创建/完成/删除。
执行返回 `project_global_write_forbidden`；已有全局内容可读，项目记忆保存仍可用。
项目聊天的 Dream 正文标记也不启动后台 Dream、不发出 `ev_dream`；普通、流式及工具流式路径一致，断连不绕过。
直接自有 MCP 的合同是角色全局范围，不能当作项目上下文的旁路使用。

### 角色—项目—会话—消息

完整身份是 `(character_id, project_id?, conversation_id, message_id)`，数字记忆 ID 也必须和角色 ID 成对保存。
不同角色可以重复使用同名项目、相同会话 ID、消息 ID或数字记忆 ID，它们是不同对象，不能跨角色用裸 ID 关联。
同角色更换会话 ID 仍检索该角色长期记忆；改名或换模型不改变归属。
项目 ID 必须在选定角色库里存在；已删除、未知项目、与会话元数据冲突，聊天返回 409，停止注入和模型调用。
会话首次真实聊天或同步创建/写入会绑定项目（包括全局 null）；已绑定会话不能换项目，需新建会话 ID。
角色模式的 PATCH、PUT、JSON 导入、ZIP 恢复都在元数据写入事务内验证同一归属，普通改名不改变绑定。
未绑定的旧会话也以现有元数据和已知账本为证据；矛盾归属拒绝写入，不猜测、不重写旧行。
启动发现已存在的绑定表与会话元数据冲突时会拒绝启动，须停写并恢复一致备份后再启动。
关闭角色模式的旧单角色服务继续使用原同步语义；再次启用前须检查归属一致性。
绑定在会话删除后保留，和旧版删除墓碑一起防止过期请求改变归属。
仅使用其他角色的会话 ID 不会读取对方会话；它最多表示当前角色中一个独立的新会话。

聊天建议提供稳定的 `conversation_id`、`turn_key`，以及成对的 `user_message_id`、`assistant_message_id`。
会话 ID 不是角色 ID；不要每次切窗新建角色。上游消息重生成、删除墓碑与重置世代语义保留。

### 真实互动与辅助生成

```json
{
  "model": "your-model",
  "conversation_id": "float-chat-A-01",
  "turn_key": "turn-001",
  "user_message_id": "message-u-001",
  "assistant_message_id": "message-a-001",
  "memory_mode": "interaction",
  "messages": [{"role":"user","content":"今天发生的真实互动"}]
}
```

`memory_mode` 缺省或 `interaction`：使用该角色记忆，按原配置落账/提取。
`auxiliary`：不注入人设和记忆、不记录对话、不自动提取，且不执行网关工具；用于标题、改写、摘要草稿等。
不支持其他值；拼错返回 400。旧 `skip_system_prompt` 保留原行为，但它并不保证禁用工具，float 应使用新字段。
本轮未提供“读取记忆但绝不产生召回/工具写入”的 read-only 模式。

### 返回错误与重试

| 条件 | HTTP / code |
| --- | --- |
| 未传任何角色选择器 | 进入 default，响应头 `X-Kiwi-Character: default` |
| 显式非法角色 | 400 `invalid_character_id` |
| 不支持的 query 选择器、重复角色头、非法 JSON | 400 对应明确 code |
| 非支持的非空请求体媒体类型 | 415 `unsupported_media_type` |
| 同步项目 ID 非法格式 | 400 `invalid_project_id` |
| 路径/请求头/JSON 角色冲突 | 409 `character_mismatch` |
| 未创建角色 | 404 `character_not_found` |
| 已停用角色 | 410 `character_deleted` |
| 重复创建/已达容量限制 | 409 `character_exists` / `character_capacity_reached` |
| 项目归属不合法 | 409 `invalid_project_ownership` |
| 已绑定会话被用于其他项目 | 409 `session_project_mismatch` |
| 错角色备份 | 409 `backup_character_mismatch` |
| worker 未就绪、数据库/服务失败 | 503；不能重试到 default |

创建失败可能留下 provisioning/active 记录，查 GET 后处理，重启可恢复未完成建库。
网络断开不保证请求未落账；稳定 turn/message ID 可使用已有幂等机制，手工添加记忆没有新增幂等键。
未知角色不会按需自动创建；角色删除后不能复用 ID 创建另一人。

### 删除与导入边界

- 删除会话仅在当前角色执行上游对话/消息/墓碑语义；不会自动承诺删除所有已派生记忆，须按上游来源与删除规则处理。
- 删除记忆仅影响当前角色；锁定保护仍有效。清空/重置接口也只影响当前角色。
- 删除角色为**停用归档**：停止进程、拒绝后续访问，保留库和 ID 墓碑。不自动 DROP DATABASE；default 禁止停用。
  永久擦除与恢复归档角色接口尚未实现，需管理员在备份后另行安排。
- JSON 批量导入中的归属冲突计入 `rejected`，以 `session_project_mismatch` / `invalid_project_ownership` 等受控码报告；其他实体可继续导入。
- ZIP 按对话事务恢复，归属冲突不改该会话及其消息、墓碑；计入 `failed_conversations` 与 `scope_errors`。客户端必须检查计数，不能只看 HTTP 200。
- ZIP 增加 `character.json`。A 的包不能导入 B；无此文件的旧包仅可导入 default。
  显式 JSON 事件/同步导入始终属于请求选择的角色，调用方负责素材来源，不能据此宣称内容层防冒充。
- ZIP 沿用上游选择性导出/追加恢复能力（不是完整数据库快照），本轮额外保留记忆的 `project_id`。
  热度/全部派生表等完整灾备应使用 pg_dump，不以 ZIP 替代。

## 已登记缺口与 float 下一步

1. float 保留稳定 `characterId`，在可信服务端映射为 kiwi 角色 ID，先显式创建角色；每次调用固定选择该角色。
2. 私聊落到该角色；群聊、朋友圈、日记、剧情等先由 float 明确参与者与可见范围，逐角色分发允许其知道的经历。
   kiwi 本轮没有共享库、群聊传播或自动成员映射。
3. 设计独立的跨应用事件接口仍为待办：需要 source_app/source_event_id、事件时间、参与者/可见性、幂等、修订/撤回及派生记忆来源链。
   当前可手工添加或同步对话，但不要伪装成已经完成统一事件协议。
   **日历读取同步后的 `chat_messages`，不直接消费聊天请求产生的原始事件账本。**
   float 若要使用日历，需同步允许该角色看到的会话和消息；只调用聊天接口不等于完成日历素材接入。
4. 角色模式暂时禁用外部 MCP（包括请求传入的服务器），因为其读写没有验证过角色合同；自有 MCP/工具可用。
   旧单角色启动不受此限制。未来接入外部工具必须先明确角色隔离和副作用范围。
5. 客户端工具列表与网关工具列表在部分流式路径没有完整合并，仍为独立兼容性待办；不能据此声称 float 全部工具已兼容。
6. 项目自动提取、项目日历/项目 Dream 尚未新增；原角色内全局认知层与项目私有层的合同继续保留。
7. 用户级认证授权、横向扩容、永久角色擦除、完整事件导入、生产监控与容量测试尚未完成。

本地验证记录见 [character-isolation-verification.md](character-isolation-verification.md)。

2026-09-17 审查修复与新增测试见 [character-isolation-review-fixes.md](character-isolation-review-fixes.md)。
