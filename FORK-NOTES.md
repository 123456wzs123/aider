# FORK-NOTES — 维护台账

> 本文件是**状态性文档**：基线、已打补丁、已知问题、候选池。
> 过程性记录（为什么做、怎么做、结果）见 [MAINTENANCE-LOG.md](MAINTENANCE-LOG.md)。

## Fork 基本信息

- 上游：https://github.com/Aider-AI/aider （休眠，最后提交 2026-05-22 `5dc9490bb`）
- 本 fork：https://github.com/123456wzs123/aider
- 本地路径：`D:\目前作为文件放置\ZCode\projects\aider-fork`
- 分支模型：fork `main` = 上游 main + 补丁提交；高风险 PR 先进 `try/pr-xxxx` 分支
- venv：`.venv313`（主，Python 3.13.15）/ `.venv314`（辅，Python 3.14.7）

## 基线记录（改动前上游代码在 3.13 上的测试结果）

> 目的：区分"本来就坏"和"我们改坏"。基线未跑完前，不判定任何失败是我们引入的。

- 状态：**已完成（2026-11-10）**
- 命令：`.venv313/Scripts/python.exe -m pytest tests/basic -q`
- 结果：**472 passed, 5 skipped, 67 subtests passed, 6:01，退出码 0 —— 基线全绿**
- 结论：上游 main（`5dc9490bb`）在 Python 3.13.15 上测试套件完全通过；之后出现任何测试
  失败默认视为我们引入的回归，须排查后才能合并

## 已打补丁台账

| PR | 日期 | 内容 | 状态 |
|---|---|---|---|
| #5974 | 2026-11-10 | 阻止模型把文件写逃出仓库（安全） | 已合并 main，测试绿 |
| #5967 | 2026-11-08 | 修复 editblock 缩进导致的编辑损坏 | 已合并 main，测试绿 |
| #4987 | 2026-11-10 | 清理孤立代理字符（Windows/中文崩溃） | 已合并 main，测试绿 |
| #5950 | 2026-11-10 | 元数据缓存拉取失败不毁本地缓存 | 已合并 main，测试绿 |
| #5964 | 2026-11-10 | 配置路径 `~` 展开 | 已合并 main，测试绿 |
| #5966 | 2026-11-10 | 缺 aiohttp 友好补救 + 349 行测试 | 已合并 main，测试绿 |
| fork fix | 2026-11-11 | local 元数据 override 赢过已加载 litellm（上游竞态 bug） | 已修，组合测试绿 |
| #5947 | 2026-11-11 | whole-file 嵌套代码块不再截断文件 | 已合并 main |
| #5565 | 2026-11-11 | `/tokens` f-string 漏 f 前缀 | 已合并 main |
| #5939 | 2026-11-11 | Windows 跨盘符 commonpath ValueError | 已合并 main |
| #5945 | 2026-11-11 | litellm 新异常类型不再崩启动 | 已合并 main |
| #5915 | 2026-11-11 | 配置候选过滤目录路径 | 已合并 main |
| #5952 | 2026-11-11 | legacy Windows 控制台 ASCII 兜底 | 已合并 main |
| #5809 | 2026-11-11 | 提示词语言指令 + /chat-language（自修：删死变量、补 9 测试） | 已合并 main |


## Agent 壳：aider-agent（2026-11-11）

> 定位：aider 本体是"一条消息 → 编辑 → 结束"的单轮工具；本壳在其编辑引擎上加了真正的
> 工具循环，模型可以自主探索、跑测试、按失败迭代。第一期只做壳，不含 MCP/插件/skills/子 agent。

- 入口：`aider-agent`（`aider.agent.cli:main`），也可 `python -m aider.agent`
- 结构：`aider/agent/` 下 runtime / llm / tools / edit_engine / ui / prompt /
  repo_context / permissions / session
- 编辑：`edit_file` 工具 → `find_original_update_blocks` + `replace_most_similar_chunk`
  （继承 #5967/#5947 修复），**每文件原子**应用，失败回传 did-you-mean
- 工具：read_file / list_files / search_text / edit_file / write_file / run_command
- 权限：读与编辑放行（路径必须在仓库内）；run_command 默认询问，`--yes` 放行；
  `--dry-run` 全部只打印
- 上下文：按"回合"裁剪，保留 system、repo map、首条任务；旧工具结果头尾截断
- 仓库映射：复用 RepoMap；**缓存写到 `~/.aider-agent/tags-cache/`，不污染仓库**
- 会话：`~/.aider-agent/sessions/<repo-hash>/*.json`，`--continue` 恢复最近一次
- 常用：`aider-agent -m "修复测试" --yes --model openai/...`
- 明确未做：插件、skills、子 agent、全屏 TUI、摘要压缩

### MCP 支持（2026-11-12）

- 传输：**stdio**（npx/uvx/python 等本地子进程）+ **Streamable HTTP**（远程 URL，
  支持 `Mcp-Session-Id`、`MCP-Protocol-Version`、JSON/SSE 响应）；旧版 HTTP+SSE
  与 OAuth 未做
- 配置：兼容 Claude Desktop 的 `{"mcpServers": {...}}`，字段 `command/args/env/cwd`
  或 `url/headers`，可选 `timeout`、`disabled`、`approval`；`${VAR}` 从环境变量展开。
  发现顺序（同名后者覆盖）：`~/.aider-agent/mcp.json` → `<repo>/.aider-agent/mcp.json`
  → `<repo>/.mcp.json` → `--mcp-config`
- 工具：服务端 `tools/list` 自动注册为 `mcp__<server>__<tool>`（清洗并去重，≤64 字符），
  显示成 `server · tool`；调用结果按 content 块渲染（image/audio/resource 用占位符），
  `isError` 回传为失败观察
- 权限：**默认放行**（用户装 MCP 就是要用的）；`--ask-mcp` 或服务端配置
  `"approval": "ask"` / `["tool", ...]` 改为逐次询问（y/n/a，a 记住本会话）；
  `--dry-run` 只打印；`--yes` 全放行
- 项目配置（仓库里的 `.mcp.json` 等）默认需要逐 server 确认，`--trust-project-mcp`
  或 `--yes` 跳过确认；`--mcp-config` 视为可信
- 故障隔离：单 server 连接失败只告警不退出；`/mcp` 查看状态与工具数；stderr 尾部
  随错误回传；退出时杀进程树（Windows `taskkill /T`，POSIX `killpg`）
- 例：`{"mcpServers": {"fs": {"command": "npx", "args": ["-y",
  "@modelcontextprotocol/server-filesystem", "."]}, "docs": {"url":
  "https://mcp.context7.com/mcp"}}}`
- 边界：通过 MCP 改的文件不参与自动提交（模型提示词已说明，代码改动仍建议 edit_file）；
  旧版 SSE、OAuth、resources/prompts 当工具、自动重启均未做

验证：103 个 agent 测试（76 壳 + 27 MCP）；真实服务器冒烟：`npx
@modelcontextprotocol/server-filesystem`（16 工具，真实读目录）与远程
DeepWiki/Context7（3/2 工具，Context7 实际调用成功）；真实模型（opencode-go
`deepseek-v4.1-flash`）在同一轮里先用 filesystem MCP 列目录/读文件，再用 edit_file
修复 `a - b` → `a + b`，run_command 得到 2 passed，自动提交并中文总结。双 venv 全量
621/621 passed（5 skipped）；**CI 全绿**（pre-commit + Ubuntu/Windows 3.10–3.14，
提交 `64d3117bc`）。

## 已知问题清单

1. **上游 CI 环境漂移**（2026-11-11 修复）：black 23.3 在新 Python 上崩溃（已升 25.1）、
   Playwright 不支持 ubuntu26.04 runner（已加 skipif）、CI 无音频服务（已加 skipif）、
   example.com 内容改版（断言已稳健化）。详见 MAINTENANCE-LOG。
2. **Docker Build Test 失败**（待查）：首次 CI 中 Docker workflow 失败，未深入排查；
   该 workflow 与代码质量无直接关系，暂时记录不阻塞。
3. **上游 model 设置冻结在 2026-05**：新模型（如 opencode-go 的 deepseek-v4.1-flash）
   需在 `~/.aider.model.settings.yml` 里显式指定 `edit_format: diff`，否则 fallback
   到 whole 模式。本地 home 配置已处理。
4. **aider-agent 的运行数据在 `~/.aider-agent/`**（history、sessions、tags-cache），
   目前没有自动清理策略；长期使用需手动删旧 session。
5. **run_command 在 Windows 走 cmd.exe**：模型若输出 bash 语法会先失败，需自行读错误
   再修正；系统提示已声明平台 shell。
6. **项目级 MCP 配置默认需要确认**：直接跑 `aider-agent -m ...` 时若仓库自带
   `.mcp.json`，非交互环境会跳过（除非 `--trust-project-mcp` / `--yes`）；这是防止
   克隆来的仓库自动执行任意命令。
7. **远程 MCP 走系统代理**：`HTTPS_PROXY` 配了但代理没开时，远程 server 会连接失败；
   回环地址（localhost/127.x）已自动绕过代理。stdio server 继承完整环境变量。
8. **旧 session 可能引用已删除的 MCP 工具**：`--continue` 恢复后模型若再调旧工具，
   会得到 unknown tool 观察，可继续对话修正；不影响启动。
9. **fork 的 GitHub Actions 曾暂停（2026-11-12，已恢复）**：Actions 页曾提示
   "Workflows are not currently being run on this fork due to the scale of GitHub Actions
   usage. A repository maintainer can choose to re-enable these."。需维护者在网页
   Actions 页点启用（UI-only，REST API 无对应端点）。启用后 push 恢复触发；注意
   **空提交不会触发带 `paths-ignore` 的 Ubuntu/Windows 矩阵**，需带一个真实文件变更。
   最终 CI 全绿（提交 `64d3117bc`）。

## 候选池

> 筛选标准：CI 绿、带测试、改动 ≤6 文件、与优先方向（编辑质量/兼容）相关。

待每周扫描填充。首批候选（2026-11-10 确认）：
#5974 #5967 #4987 #5950 #5964 #5966（已进首批）

- **#3756**（Python 3.13 支持依赖调整）：**已评估，不采用**（2026-11-11）。上游 main 的
  requires-python 已是 >=3.10,<3.15，且 3.13/3.14 两套 venv 实测全绿，该 PR 是 2025-04
  对当时依赖的重编译，打上反而是倒退风险。
- 第二轮已采纳（2026-11-11）：#5947 #5565 #5939 #5945 #5915 #5952（见台账）。
- **#5782 已拒绝**：与 #5945 同问题，方案不如 #5945 完整。
- **#5809 已自修采纳**：方向好（不再强制系统语言、加 /chat-language）；上游基础设施
  本就存在，原 PR 仅遗留一个死变量，删除并补 9 个测试后合并。
- 剩余候选（未评估）：#5959（--files-delta 省 token）、#5954（Pygments 样式）、
  #5692（子进程环境凭据清理，+293/4f 较大）、#5963/#5957（fail-closed EOF，
  CI pending）、#5958/#5731/#5943/#5940（文档类，暂不打）。
