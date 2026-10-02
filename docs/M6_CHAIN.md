# M6 本地登记链

`chain/contracts/InventionRegistry.sol` 是非金融 v1 登记合约参考实现；`wf.chain.InventionRegistry` 是无 RPC、无钱包、chain ID 31337 的确定性本地模型。两者只登记 `packHash`、父登记、许可证摘要和内容 URI，不参与 tick、库存或权限。

实现覆盖零 hash、父不存在/重复/过多、父顺序、URI 协议与长度、重复登记幂等、冲突参数、同包多登记者、仅登记者撤销和无价值转移。ID 使用 `keccak256(abi.encode("WF:invention:v1", chainid, address(this), msg.sender, packHash))`；Python 内置 Keccak-256 向量测试，避免误用 NIST SHA3-256。

默认 `CHAIN_MODE=off`。本仓库不自动安装 Solidity 编译器、不连接 RPC、不接受私钥、不广播交易，也不执行测试网或主网部署。真正的钱包签名、链重组索引和 finality 仍需在明确批准后单独接入。

```powershell
.\work\python311\python.exe -m pytest -q tests\chain
```
