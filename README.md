# WorldForge — Codex 设计交接包

**设计日期：2026-10-02；版本：0.1；状态：离线可运行实现。**

玩家以自然语言提出发明，AI编译为可验证规则包；可信内核执行，其他玩家可使用、改造与复现。区块链仅作为可关闭的登记与谱系层。WorldForge为内部代号，公开名称需要更换。

## 阅读入口

| 文件 | 作用 |
|---|---|
| `CODEX_START.md` | 可直接复制给Codex的首批任务 |
| `AGENTS.md` | 实施边界、安全约束、测试与交付标准 |
| `WorldForge_DESIGN.md` | 产品、引擎、DSL、AI、验证、版本和链的主规格 |
| `docs/API_AND_DATA.md` | 接口、命令、权限、存储、同步与恢复 |
| `docs/BLOCKCHAIN.md` | 合约接口、签名、登记、内容可用性、重组和边界 |
| `docs/ACCEPTANCE.md` | M0–M6验收用例与演示真实性要求 |
| `docs/SECURITY_AND_RELEASE.md` | 威胁模型、资源上限和发布门槛 |
| `docs/REFERENCES.md` | 官方参考资料与核查范围 |
| `schemas/blueprint.schema.json` | WF-IR 0.1结构规范，额外语义检查仍必需 |
| `examples/blueprints/` | 三个合法蓝图样例 |
| `examples/invalid/` | 六个应拒绝的恶意/错误样例 |
| `examples/fixtures/` | 初始世界、目录、演示命令与推导预期 |
| `test_vectors/` | 规范化字节与蓝图hash的跨语言对照 |
| `tools/validate_handoff.py` | 本设计包校验器；不是游戏内核 |
| `VALIDATION_REPORT.md` | 实际执行过的设计包校验范围与结果 |

## 第一批怎么交给Codex

`CODEX_START.md` 保留了最初从 M0–M2 开始的交接任务。当前仓库已经在此基础上继续实现了 M3–M6 的可运行离线部分，不把真实模型、钱包、RPC 或部署伪装成已完成能力。

当前实现已覆盖离线核心、Web/UI、规则包、受控模型适配器和本地登记链模型：严格验证蓝图 → 放置与扣材料 → 12tick产出4份面粉 → 能源与物料守恒 → 保存与回放一致。真实模型调用、钱包、RPC、部署和主网仍按离线边界关闭。

真实模型适配器在M4中保留为显式关闭的边界；分享规则包在M5，本地登记链模型在M6。完成离线fixture演示不等于已具备自由输入的AI发明能力。

## 本包自己可运行的校验

在普通 Windows Python 3.11 环境中：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,web]"
$env:PYTHONPATH = "$PWD\src"
& .\.venv\Scripts\python.exe tools\validate_handoff.py
```

它检查3个合法蓝图、6个拒绝样例、4组规范化向量和3个蓝图摘要；不运行模拟引擎、游戏服务器、真实模型或合约，也不联网。缺依赖只报告，不自动安装。

`examples/fixtures/expected_demo.json` 是根据规格推导出的预期值，不是引擎实测输出。应用代码、测试和本地运行记录由实现阶段生成；本仓库不包含链上部署地址，也不把设计包校验结果冒充为引擎或合约验收。
