# 区块链扩展规格：登记而不是把游戏金融化

状态：M6 可选；默认关闭。不在首轮 Codex 实施范围内。没有审计完成的合约代码或主网部署承诺。

## 1. 架构决定

游戏模拟、包验证、玩家权限与库存均链下。链只接收人类明确批准的公开规则包登记；后续可增加 checkpoint commitment。世界不用钱包也能创建、游玩、保存、导出。

为什么需要链：不同运营者可读取同一登记与创作谱系；内容摘要和登记事件可独立核查。为什么不必一开始用链：单机版本可通过内容寻址、签名清单和公开备份达到主要产品目标。登记合约不验证作者真实身份、原创性、IR 安全性或跨引擎兼容性。

拒绝范围：ERC-20、挖矿、收益承诺、土地预售、借贷、赌局、跨链桥、自动交易、游戏库存兑付、托管用户私钥。v1 无价值转移函数、无代理升级、无外部任意调用、无 delegatecall、无在线部署 AI 生成合约。

## 2. InventionRegistry v1 数据

```solidity
// 接口草案；不是完整、安全审计或已编译实现。
struct Invention {
    address registrant;
    bytes32 packHash;
    bytes32 licenseHash;
    uint64 registeredAtBlock;
    uint8 parentCount;
    bool deprecated;
}

function register(
    bytes32 packHash,
    bytes32[] calldata parentIds,
    bytes32 licenseHash,
    string calldata contentURI
) external returns (bytes32 inventionId);

function deprecate(bytes32 inventionId) external;
function getInvention(bytes32 inventionId) external view returns (Invention memory);
function getParents(bytes32 inventionId) external view returns (bytes32[] memory);
function getContentURI(bytes32 inventionId) external view returns (string memory);
```

事件：`InventionRegistered(inventionId, registrant, packHash, parentIds, licenseHash, contentURI)` 和 `InventionDeprecated(inventionId, registrant)`。合理设置 indexed 字段，动态数组放 data；注册事件中记录 parent 列表不等于把同一数据递归复制进所有后代。

ID 精确定义：

```text
inventionId = keccak256(abi.encode(
  "WF:invention:v1", block.chainid, address(this), msg.sender, packHash
))
```

只用 `abi.encode`，不混用 packed 编码。此 ID 含 chain 与合约域；packHash 是 SHA-256 内容摘要，二者不是同一种 hash。客户端必须使用与 Solidity ABI 编码一致的成熟库并补跨语言测试。

## 3. register 行为

1. packHash、licenseHash 不得为零；parentIds 长度 <=4，严格升序且去重，每项必须已存在。
2. contentURI UTF-8 字节长度 <=256；合约只做尺寸/基础格式限制，不取回内容。v1 客户端只提供 `ipfs://` 或经过允许的 HTTPS 内容位置；HTTP 解析、SSRF 防护与内容 hash 校验在客户端/内容服务完成。拒绝 javascript/data/file 等可执行或本地协议。
3. 同一个 `(msg.sender, packHash)` 重复提交返回既有 ID 且不改变字段、也不再发注册事件；如果携带不同的 parentIds/licenseHash/URI，则明确 revert `ConflictingRegistration`。相同内容更新展示位置需要未来独立 locator 设计，不在 v1 原地改写。
4. 保存 immutable register 数据，发事件。无收款、无分账、无付款调用。`register` 为 nonpayable。
5. 其他地址登记同一 packHash 不覆盖原记录，也不能阻止原作者后来登记；前端称其为“另一登记声明”，不称其原创归属。
6. parent 指针表示登记者声明的派生关系。父项存在只证明有该登记，不证明许可允许、实际用了父代码或涵盖全部先前工作。
7. 合约不解析 manifest；因此前端 `content_verified` 还必须检查 manifest bytes 的 packHash、登记者声明、license bytes 的 hash、parents 所指包与 manifest parent hashes 的对应关系。不同会显示冲突，绝不标为验证通过。
8. contentURI 指向的内容不可用时记录仍存在，但页面标 `content_unavailable`，不能说内容永久可取。

`deprecate` 仅允许 registrant，把当前记录标 deprecated，不能删历史或更改 hash。deprecated 父包依然可被引用，以保留历史；公共安装器自行决定是否允许，默认提示且重新验证。没有“管理员能偷偷改作者”的接口。

## 4. 钱包与请求安全

v1 钱包只用于登记。普通游戏登录与钱包绑定分离；浏览器确认链 ID、合约地址、packHash、父包和 URI 后自行签交易。后端只提供待签参数和读取状态，不接受或记录 seed/private key。

钱包绑定若用于账户关联必须使用单次挑战、域名、chain ID、有效期并检查签名；不要只比较前端传来的 address。钱包关联成功也不授予他人世界 owner 权限。

以后支持 EIP-712 relayer 时，domain 必须包含 name/version/chainId/verifyingContract，消息需包含 registrant、packHash、parentIdsHash、licenseHash、uriHash、nonce、deadline。签名校验采用成熟实现，智能合约钱包支持另行测试。重复 nonce 明确拒绝；转发者不得改 registrant 或接收人。EIP-712 不自带 replay protection。[R9]

## 5. 内容存储与 outbox

登记前已经生成完整 manifest，并确认所有文件存在且摘要正确。manifest 的生成需要真实 engine artifact hash；不允许把全零或虚构 engine hash 当成完成的包。

流程：

```text
本地内容写入成功
→ SQLite publication_intent=unsigned
→ UI 展示待签信息
→ 用户钱包提交
→ UI 记录 tx_hash（只作为提示）
→ 后端读取链，验证合约/事件/参数
→ included
→ finality policy 满足后 finalized
```

v1 outbox 不自动签名，也不自动广播。提交后的浏览器回报不能作为成功证据；索引器必须核实事件。RPC 宕机只影响 publication status，不阻塞 tick 或包的本地使用。

IPFS 为可选存储适配器，不是核心依赖。上传本身不等于永久存储；必须有明确 pinning/备份责任并做定期可取回检查。[R10] 不把原始用户提示、聊天、私密世界存档上传公开网络。

## 6. 网络与部署阶段

`CHAIN_MODE=off` 为默认。`local` 模式连接显式配置的 loopback 开发链，预期 chain ID 31337；必须同时验证实际 chain ID 和 RPC 主机，不可因为某个远程节点自称 31337 就发送开发密钥。公开的开发账户只用于隔离本地测试，从不在其他网络复用。

`testnet` 必须经用户明确批准，网络建议 Base Sepolia，官方当前 chain ID 84532。[R8] 配置 `expected_chain_id`、合约 allowlist、RPC endpoint、部署前代码摘要、compiler version、gas 模拟结果；不使用自动 faucet、桥接或资金转移脚本。

`mainnet` 当前显式 unsupported。Solidity 编译器与依赖使用实现时已发布的兼容版本并锁定，不能把官网 `latest` 的 develop 版本当成稳定发布。参考 Solidity 安全指南，限制循环、访问权限与外部调用；跨合约权限使用成熟模式。[R13][R14]

## 7. 重组、最终性与状态索引

记录 tx_hash、block_number、block_hash、log_index 与 canonical 标记。每次推进索引核对父区块连续性；发现冲突回退到最近共同祖先，撤销对应投影，重新抓取事件。读链请求重试有上限与退避。

`included` 与 `finalized` 分开显示。JSON-RPC 的 safe/finalized 标签存在，但不能假定所有节点实现与语义完全一致；链适配器要实际测试并记录策略。[R11] fallback 只显示 confirmations，不把固定确认数标为绝对不可逆。

游戏状态与链索引分别持久化；链重组不得复制库存、回退加工或恢复旧会话权限。二者没有原子跨系统提交承诺。

## 8. 可选 WorldCheckpointRegistry（M6 后续，不是 v1 必做）

每个 world publication stream 由 `(publisher, local_world_key)` 派生独立 ID。注册世界时保存 publisher；只能 publisher 提交 checkpoint。checkpoint 包含 branchId、tick、previousCheckpoint、checkpointCommitment；tick 在同一 branch 内严格增长，previousCheckpoint 必须等于当前 head。分叉必须创建独立 branch，并登记父 checkpoint，不能覆盖旧 head。

commitment 绑定 engine artifact hash、catalog hash、policy hash、config hash、ruleset hash、state hash、commands head、snapshot file hash 和 data locator hash。读取者须取得数据并复算。

它只证明发布者在某次链上记录中承诺了某数据摘要，不能证明模拟正确、日志没有审查、游戏从未作弊或内容持续可用。没有证明系统时前端必须标记 `publisher_asserted`，不能标 `trustless_verified`。

## 9. NFT 与分账的明确边界

首版无需 NFT。规则蓝图是可复制知识；执行资格由世界策略决定。把蓝图 token 化不会自动防复制，也不会让一个规则包在不兼容引擎中运行。

后续真有收藏需求才考虑 ERC-721/1155 独立契约与许可条款，不能将它们塞进核心物料账本。版税的 ERC-2981 只提供查询信息，不能保证所有平台或离线使用都付款。[R12] 没有验证过的实际需求前不设计经济模型。

## 10. 链模块验收

必须测试：重复登记幂等；冲突参数拒绝；同内容不同地址并存；父不存在/重复/过多拒绝；零 hash 拒绝；URI 超长拒绝；其他人不能 deprecate；无资金转移函数；错误 chain ID 阻止签名；前端伪造 tx_hash 无法标成功；重组后徽标撤回而游戏状态不变；内容 hash 错误拒绝安装；断 RPC 游戏仍运行；无钱包完整游戏仍可玩。

若启用 EIP-712，还需签名重放、过期、换链、换合约、修改父列表与错误 signer 测试。若启用 checkpoint，还需未授权 publisher、倒退 tick、错误 parent head 和 fork 绑定测试。

上线前独立安全审查是发布门槛；测试全绿不等于审计完成，更不等于可以托管真实资金。
