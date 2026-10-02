# M4 模型适配与评测

`wf.adapters.model` 提供 `InventionProvider` 边界，模型输出始终是不可信 JSON 数据。`FixtureProvider` 只接受明确的 `scenario_id`，未知离线请求返回 `OFFLINE_FIXTURE_ONLY`；`ManualProvider` 只表示人工提交；`RemoteProvider` 默认关闭，只有显式启用、模型和端点通过 allow-list、并注入 transport 后才会调用外部实现。核心不导入模型 SDK，也不会在默认配置下联网。

`InventionCompiler` 对每个请求执行严格编译、内容哈希、独立沙箱验证和有限修复。最多执行初次请求加两次修复；每轮记录模型 ID、请求 ID、输入/输出 token、延迟、候选 hash、验证报告 hash、错误码和终态。请求、候选和验证报告均不能绕过本地 `compile_blueprint` 或 `verify_blueprint`。累计 token、单轮 token、延迟和请求数超过预算时拒绝并保留诊断。

支持范围外的传送、任意脚本、修改环境源额度、永动/无限产出、真实流体、跨实体全局控制等请求在 provider 调用前返回 `E_UNSUPPORTED_INTENT`。输入中的密钥和授权头返回 `E_MODEL_SENSITIVE_INPUT`。这只是 DSL 能力拒绝，不把模型说明当作安全证明。

`build_m4_evaluation_suite()` 返回 30 条独立任务：10 条基础组合、10 条未出现在 fixture 列表中的组合、10 条必须拒绝的请求。`evaluate_suite()` 保存每条原始 prompt、候选、候选 hash、验证报告、修复轮数和终态；fixture 结果单独统计，排除在模型分母之外。

本地验证命令：

```powershell
Set-Location "<worldforge-repository>"
$env:PYTHONPATH = "$PWD\src"
& .\.venv\Scripts\python.exe -m pytest -q tests\unit\test_model_adapter.py
& .\.venv\Scripts\python.exe -m pytest -q
```

远程适配器不会因为设置了模型名称而开启；生产接入还需要明确的密钥管理、供应商协议转换、实际价格计量和外部进程级网络隔离。本轮不下载模型、不要求 GPU、不接钱包、RPC 或区块链。
