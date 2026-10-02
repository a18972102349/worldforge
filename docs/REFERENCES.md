# 官方参考资料与核查范围

核查日期：2026-10-02。下面资料用于核实相邻项目、工具边界和协议性质；本文的架构、数值配额和里程碑属于设计建议，不是这些来源对本项目的认证，也不表示实现已完成。

- **R1 — Worldforge 官方项目**：https://www.worldforge.org/ 。确认名称已有实际开源项目使用；不据此宣称对方与本项目合作或为本项目背书。
- **R2 — Pydantic strict mode**：https://docs.pydantic.dev/latest/concepts/strict_mode/ 。严格类型验证参考；仍需处理重复JSON键、浮点token与语义校验。
- **R3 — FastAPI WebSockets**：https://fastapi.tiangolo.com/advanced/websockets/ 。通信实现参考；权限、重连、幂等和actor语义为本项目额外设计。
- **R4 — PixiJS introduction**：https://pixijs.com/8.x/guides/getting-started/intro 。2D渲染选择参考，不当作确定性权威物理引擎。
- **R5 — OpenAI Structured Outputs**：https://developers.openai.com/api/docs/guides/structured-outputs 。结构化输出仍可能包含错误；本地完整schema和独立行为验证不可省略。并非限定本项目只能使用OpenAI模型。
- **R6 — RFC 8785, JSON Canonicalization Scheme**：https://www.rfc-editor.org/rfc/rfc8785 。规范化参考；WF-CJSON-1 是本文限定输入域的项目格式，不宣称完整JCS兼容。
- **R7 — MUD introduction**：https://mud.dev/introduction 。已有全链应用/世界框架；本规格采取链下执行、链上登记的不同范围，不宣称全链世界概念首创。
- **R8 — Base network details**：https://docs.base.org/get-started/connect-to-base 。核查时列出的Base Sepolia chain ID为84532；实现时再次核对配置，不代表已部署或资金安全评估。
- **R9 — EIP-712**：https://eips.ethereum.org/EIPS/eip-712 。类型化签名与域隔离；标准明确不包含应用层重放保护。
- **R10 — IPFS pinning**：https://docs.ipfs.tech/how-to/pin-files/ 。pinning、缓存与垃圾回收边界；CID不等于永久可用。
- **R11 — Ethereum JSON-RPC**：https://ethereum.org/en/developers/docs/apis/json-rpc/ 。safe/finalized等区块标签参考；具体链与RPC支持需单独验证。
- **R12 — ERC-2981**：https://eips.ethereum.org/EIPS/eip-2981 。版税查询标准，支付有依赖参与方执行的边界，不保证链下使用或所有市场付款。
- **R13 — Solidity security considerations**：https://docs.soliditylang.org/en/latest/security-considerations.html 。安全开发参考；latest页面可能是develop文档，实施时选已发布兼容编译器并锁定。
- **R14 — OpenZeppelin access control**：https://docs.openzeppelin.com/contracts/5.x/access-control 。权限模式参考，不意味着本项目合约经过该项目审计。
- **R15 — Codex AGENTS.md**：https://developers.openai.com/codex/guides/agents-md 。项目级指令读取与范围参考；自动指令文件应简短，具体设计由任务显式指定阅读。

## 没有完成的外部验证

没有声称检索穷尽所有同类研究、证明全球首创、确认新名称可注册、评估任何法域的发行许可或完成智能合约安全审计。没有实际调用模型、运行本规格的模拟引擎、启动游戏服务器或广播链上交易。规格包的验证只覆盖附带样例与格式。
