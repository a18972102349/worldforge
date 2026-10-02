# M5 规则包、导入导出与分叉

`wf.pack` 生成确定性的自包含 ZIP：规范化 manifest、编译后的 blueprint、可选 LICENSE。`pack_hash` 使用 `WF:pack:v1` 域分离，压缩时间戳固定，不受 ZIP 压缩实现影响。

导入在内存中完成，不按包内路径解压；拒绝路径穿越、绝对路径、重复成员、目录、symlink、解压炸弹、外部 URL、上下文 hash 不匹配和内容篡改。服务端持久化已验证的包，安装必须引用已导入的 `pack_hash` 和对应 `blueprint_hash`。

Web API 提供包查询、规则包导入、blueprints-only/rule-pack 导出、完整世界 fork 和空闲包卸载。正在使用、已安装、有实体或有排队安装命令的包不能卸载。fork 创建新 branch，复制已提交 tick 的状态、命令和日志头，不改变源世界。

```powershell
.\work\python311\python.exe -m pytest -q tests\unit\test_pack.py tests\integration\test_web_m5.py
```
