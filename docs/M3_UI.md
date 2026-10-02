# M3 Web UI 与受控会话

`web/` 是 M3 的 React + TypeScript + PixiJS 工坊界面。规则、tick、能源和物料状态仍由 FastAPI 服务端的确定性内核运行；浏览器只提交受控命令并渲染服务端快照。默认运行模式仍是 `AI_MODE=offline`，不需要模型、GPU、钱包或链服务。

## Windows 运行

项目提供 `work/python311/python.exe` 作为已验证的 Python 3.11 运行时。首次准备依赖时，在项目根目录执行：

```powershell
$py = ".\work\python311\python.exe"
& $py -m pip install -e ".[dev,web]"

$pnpm = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\fallback\pnpm.cmd"
$env:NODE_PATH = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules"
& $pnpm --dir web install
```

如果依赖已由本地运行时缓存提供，可使用 `& $pnpm --dir web install --offline`；E2E 不会下载浏览器，而是使用本机已有的 Chrome 或 Edge。

启动后端（只绑定本机）：

```powershell
$env:PYTHONPATH = "src"
$env:WORLDFORGE_DB = (Join-Path (Get-Location) "work\worldforge.sqlite3")
& $py -m uvicorn wf.web.main:app --host 127.0.0.1 --port 8010
```

另开终端启动 Vite：

```powershell
$env:NODE_PATH = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules"
& $pnpm --dir web dev --host 127.0.0.1 --port 5173
```

打开 <http://127.0.0.1:5173/>，先用本地 actor ID 登录，再创建内置 `river_demo` fixture 世界。登录响应设置 HttpOnly `wf_session` cookie；写操作同时携带服务端返回的 CSRF token。生产部署应将本地登录替换为外部身份系统，本轮不接入外部认证。

也可以构建后用静态服务器预览：

```powershell
& $pnpm --dir web run build
& $py -m http.server 5173 --bind 127.0.0.1 --directory web\dist
```

## 当前功能

- 画布使用快照中的 `map`、`sources`、`entities[*].cell` 绘制网格和实体，PixiJS 不推导规则。
- 面板显示服务端 `energy_ledger`，包括外部提取、转换损耗、加工消耗和未使用能量。
- owner、inventor、builder、viewer 的写权限由服务端每次执行命令时复核；viewer 的写操作会被拒绝。
- 支持安装蓝图、放置实体、存入/取出物料、取消作业、owner tick、成员授予/撤销。
- 支持保存检查点、加载检查点、验证提交日志和状态哈希。
- WebSocket 推送增量状态；断线后会重连并请求全量 resync。撤销会话后，连接和后续写命令都会失效。
- `tools/run_e2e.mjs` 与 `tools/run_e2e.ps1` 启动隔离的 loopback 后端和 Vite，使用两个浏览器 context 覆盖权限、标准 demo、重连、保存和后端重启恢复。

## API 与持久化边界

生产入口 `wf.web.main:app` 使用 SQLite 持久化世界、成员关系、命令、事件、tick commit 和会话。重启后通过 `WORLDFORGE_DB` 恢复同一状态；加载不会按墙钟补 tick。队列有 1000 条上限，payload 有 64 KiB 上限，命令 ID 按 actor 隔离幂等。开发测试仍可通过 `create_app(service)` 注入内存服务，兼容 M2 的 header actor 测试。

## 已知差距

本轮仍没有实现外部登录/账号注册、完整数据库 migration 版本管理、GPU、钱包 RPC 客户端或真实模型服务调用；M4 的适配器接口、M5 规则包和 M6 本地登记模型已加入，但真实模型与链节点仍由显式配置和人工批准控制。前端生产构建会提示 PixiJS 主 chunk 超过 500 kB；这是性能优化提示，不影响功能。Python 测试环境还会显示 Starlette/httpx 的弃用警告。设计验收中的重复键错误码与现有示例存在 `E_PARSE`/`E_DUPLICATE_KEY` 文档冲突，当前保持实现和示例的 `E_DUPLICATE_KEY`，没有降低验证标准。

