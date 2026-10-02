# API、数据模型与恢复协议

状态：M2–M6 待实现规范。所有示例端点都是目标接口，不是已有线上服务。

## 1. 核心数据类型

`WorldConfig`：world_id、branch_id、owner_id、engine_release、engine_artifact_hash、capability_hash、policy_hash、map、source budgets、limits。world_id/branch_id 是应用身份，不参与实体物理行为；完整存档保留它们，simulation_state_hash 可排除它们以支持分叉等价比较。

`WorldState`：tick、rules_epoch、installed_pack_hashes、entities、mechanical_links、warehouse、resource_ledger、energy_ledger、deterministic_counters。保存 complete state 而非只保存画面。

`EntityState`：entity_id、placement_command_id、node_id、pack_hash、cell、on_hand、input_escrow、output_reserved、active_job、last_status。ID 由 `"e" + str(placement_server_seq) + '/' + node_id` 生成（server_seq 在同一世界内唯一），不能用系统时间或随机 UUID 参与 tie-break。

`Job`：job_id（实体内单调计数器）、inputs、outputs、work_required_eu、progress_eu、rate_eu_per_tick。inputs/outputs 是创建作业时固定的规则引用或拷贝；包更新不得改变现存作业。

`RulePackRecord`：pack_hash、blueprint_hash、manifest_bytes、blueprint_bytes、engine/catalog/policy hashes、parent hashes、created_by、validation_report_hash。显示名称不是身份。M0–M2 可先实现 blueprint_hash 注册，M5 加完整 pack manifest；两者必须分清，不能把未生成 manifest 的 hash 假标为 pack_hash。

`ValidationReport`：report_id、candidate_hash、context_hashes、verifier_version、suite_hash、cases、limits、outcome、diagnostics。outcome 只能通过实际 case 汇总得出。

`CommandEnvelope`：command_id、actor_id（服务器身份）、world_id、branch_id、kind、payload、server_seq、effective_tick、expected_rules_epoch（相关操作必填）。client_time 只用于诊断，不决定排序。

## 2. 命令类型

| 类型 | 内容 | 前置条件 |
|---|---|---|
| INSTALL_BLUEPRINT / INSTALL_PACK | 摘要、有效报告、expected_rules_epoch | owner，报告与上下文相符 |
| PLACE | 定义摘要、origin=[x,y] | builder，定义已安装，位置和成本通过 |
| CONNECT | 两个实体 ID 和 shaft 端口 | builder，邻接、均有端口、连接不成环 |
| DISCONNECT | 两个端点 | builder，连接存在 |
| DEPOSIT | entity_id、item、amount | builder，仓库足够、目标未超容量 |
| WITHDRAW | entity_id、item、amount | builder，只取 on_hand，不取 escrow |
| CANCEL_JOB | entity_id | builder，退回输入与释放容量，无功退款 |
| DISABLE_FUTURE_PLACEMENT | 定义摘要 | owner，不删除现存依赖 |
| SET_PAUSED | paused | owner，边界暂停；不走物理时间 |

暂停/继续由 actor 控制命令队列，暂停时仍能处理“继续”与只读请求。不得将 continue 排到永远不会推进的未来 tick。控制状态与 simulation tick 状态分别持久化；恢复后明确显示暂停状态。

M0–M2 CLI 场景文件可用 `blueprint_alias` 引用示例文件；它是测试夹具语法，入口必须先解析文件、验证并计算真实摘要，再构造生产命令。生产 API 不接受服务器文件路径或 fixture alias。

## 3. HTTP API（M3）

统一错误：

```json
{
  "error": {
    "code": "E_OUTPUT_CAPACITY",
    "message": "输出容量不足以预留一批产品",
    "path": "/nodes/1/inventory_capacities/flour",
    "request_id": "req-17",
    "retryable": false
  }
}
```

| 方法与路径 | 请求/响应摘要 | 权限 |
|---|---|---|
| GET /health | 服务可用性，不返回密钥或全配置 | 本地或有限公开 |
| POST /api/worlds | scenario_id、limits；返回 world metadata | 登录用户，配额 |
| GET /api/worlds/{id} | 元数据、rules_epoch、tick、角色 | member |
| GET /api/worlds/{id}/snapshot | 权威快照与 last_seq、hash | member |
| POST /api/worlds/{id}/commands | command_id、kind、payload | kind 对应角色 |
| GET /api/worlds/{id}/commands/{command_id} | accepted/executed/rejected + result | 提交者/owner |
| POST /api/worlds/{id}/inventions | prompt、mode、parent_hash；返回 task_id | inventor |
| GET /api/inventions/{task_id} | 流水线状态、候选与诊断 | 提交者/owner |
| POST /api/inventions/{task_id}/approve | candidate_hash、report_hash、expected_rules_epoch | owner |
| GET /api/packs/{hash} | 不可变 manifest/metadata | 世界授权或公开包 |
| POST /api/worlds/{id}/exports | checkpoint/blueprints_only | owner |
| POST /api/worlds/import | 上传受限包；不接受任意 URL | 登录用户，配额 |
| POST /api/worlds/{id}/fork | checkpoint_id；新建 branch/world | owner |
| POST /api/publications/prepare | 登记参数与客户端交易请求，不签名 | 已认证发布者，M6 |
| GET /api/publications/{id} | unsigned/submitted/included/finalized/... | 发布者或公开记录 |

发明编译为持久化后台任务是应用开发目标；不能阻塞 tick 线程。任务状态 `queued/running/needs_clarification/rejected/validated/approved/cancelled`。编译成功仅生成候选，不能自动安装。离线且收到未知自由输入时返回明确 unsupported。

创建世界时只能用内置 scenario ID 或已验证完整导入文件；不要允许请求指定服务器任意目录。导出快照必须绑定已提交 tick 边界。

## 4. 幂等、排序与授权

角色能力明确为：owner=读取/提案/建造/安装批准/管理；builder=读取/提案/建造；inventor=读取/提案；viewer=只读。批准安装只允许owner；builder不能因提出蓝图而自我批准。

幂等键为 `(world_id, actor_id, command_id)`，数据库唯一索引。payload 先规范化后 hash；相同键不同 hash 返回 409。命令持久化成功后才返回 accepted。执行前再次检查当前 membership，不信任入队时已被撤销的权限。

WorldActor 用锁/邮箱封闭当前 tick 命令批次。此边界之后进入的命令获得下一可执行 tick，不得插进已计算的 tick。`server_seq` 全世界单调递增，命令排序与回放只使用服务器记录。

首次执行结果必须持久化。客户端超时后查询已有 command_id，不能盲目生成新 ID 重试。HTTP 200/202 的 accepted 不是已扣费、已加工或已安装。

网络传输顺序可能改变 accepted seq，这是不同输入历史，不属于模拟不确定性。同一最终日志重放必须一致。

## 5. WebSocket

端点 `/ws/worlds/{world_id}`。只订阅已提交状态事件，不通过 WS 写游戏命令。默认消息限制 64KiB；完整大快照使用 HTTP。

订阅发送 `{"type":"subscribe","last_seq":123}`。服务器回：

```json
{
  "type": "world_delta",
  "world_id": "world-demo",
  "branch_id": "branch-main",
  "event_seq": 124,
  "tick": 12,
  "state_hash": "64个小写十六进制字符",
  "changes": [],
  "events": []
}
```

没有连续日志时发送 `resync_required`，客户端拉完整快照，清空旧插值。entity version 或 rules hash 不匹配也必须重同步。慢消费者有有界队列；丢弃旧渲染 delta 后明确要求 resync，绝不丢弃服务器命令。

使用同站 session cookie 或短时订阅票据，避免长效 token 放查询字符串被记录。WebSocket 检查 Origin、身份与成员权限。若改为 token 协议，必须文档化生命周期及撤销策略。

## 6. SQLite 表

以下是逻辑模式，Codex 需通过真实 migration 建表并测试升级：

- worlds(id PK, branch_id, owner_id, engine_hash, capability_hash, policy_hash, config_json, created_at)
- memberships(world_id, actor_id, role, revoked_at, PK(world_id,actor_id))
- commands(world_id, actor_id, command_id, payload_hash, payload_json, server_seq, effective_tick, status, result_json, UNIQUE(world_id,actor_id,command_id), UNIQUE(world_id,server_seq))
- tick_commits(world_id, branch_id, tick, state_hash, previous_head, log_head, PK(world_id,branch_id,tick))
- events(world_id, branch_id, event_seq, tick, ordinal, type, payload_json, UNIQUE(world_id,branch_id,event_seq))
- snapshots(world_id, branch_id, tick, state_hash, bytes_path, bytes_hash, engine_hash, config_hash)
- blueprints(blueprint_hash PK, canonical_json, ir_version, capability_hash)
- packs(pack_hash PK, manifest_json, blueprint_hash, content_path)
- validations(report_hash PK, candidate_hash, context_hashes_json, report_json)
- inventions(task_id PK, actor_id, world_id, status, mode, request_json, attempts, candidate_hash)
- publication_intents(id PK, actor_id, pack_hash, chain_id, contract, status, tx_hash, last_error)
- chain_events(chain_id, contract, tx_hash, log_index, block_number, block_hash, canonical, payload_json, UNIQUE(chain_id,contract,tx_hash,log_index))

墙钟时间只用于审计/排序展示，不能回流影响引擎。通过预编译参数写 SQL；不允许候选提供 SQL 片段。

M2 可先只建所需 worlds/commands/tick_commits/events/snapshots/blueprints/validations，避免空表与空 API。

## 7. 日志、hash 与恢复

定义：

```text
config_hash = SHA256("WF:config:v1\0" || CJSON(config_for_replay))
H_0 = SHA256("WF:genesis:v1\0" || CJSON({config_hash, state_hash_at_tick_0}))
H_t = SHA256("WF:commit:v1\0" || CJSON({
  previous_head: H_(t-1),
  tick: t,
  commands: ordered_canonical_command_records,
  events: ordered_deterministic_events,
  state_hash: hash_of_S_t
}))
```

日志公式中不包含 model latency、随机 request ID、墙钟、链确认或数据库自增的非确定性附加字段。events 由确定性 engine 产生，事件序号以固定规则分配。

保存算法：先把快照写临时文件、flush 并原子 rename，再在 DB 事务记录该文件摘要和对应 tick；或者直接存 DB blob。不得 DB 指向半写文件。大文件独立持久化与 DB 提交之间的孤儿文件允许由 GC 清理，但不能出现已确认快照缺文件。

恢复算法：检查 engine/catalog/config 对应版本；加载最近有效快照并验证 hash；读取后续已提交命令；逐 tick 重新计算，并对照 events/state_hash/log_head；遇到不匹配停止，报告首个 tick。未提交的临时 tick 丢弃；accepted 但未执行命令重新入队时必须保留原 seq 与 scheduled tick，必要时进入恢复流程重新计算，而不能重复执行已提交命令。

服务崩溃后不得按现实过去了多少秒自动补生产。v0 恢复从最近提交 tick 继续；离线挂机结算不在当前规则内。

## 8. 端口与启动

目标 CLI：`python -m wf.cli demo --offline --ticks 12 --output ./runs/demo`。
目标回放：`python -m wf.cli replay --run ./runs/demo`。
M3 API 默认 `127.0.0.1:8010`，UI 默认 `127.0.0.1:5173`，均可配置。端口冲突只提示如何换端口，程序不能擅自终止其他服务。
