# WorldForge 本地登记链模块

`contracts/InventionRegistry.sol` 是 M6 v1 的非金融登记合约源码；它没有收款、代币、升级、`delegatecall`、任意外部调用或私钥处理。

Python 的 `wf.chain.InventionRegistry` 是同一接口的离线本地开发链模型，默认 chain ID 为 `31337`，用于在没有 RPC、钱包和 Solidity 编译器时验证行为。`LocalDevelopmentChain` 明确拒绝远程 RPC，普通游戏不会依赖它。

```powershell
.\work\python311\python.exe -m pytest -q tests\chain
```

若本机另行安装并锁定 Solidity 编译器，可对 Solidity 文件进行编译；本仓库不自动下载编译器，也不连接测试网或主网。
