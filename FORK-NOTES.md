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


## 已知问题清单

1. **上游 CI 环境漂移**（2026-11-11 修复）：black 23.3 在新 Python 上崩溃（已升 25.1）、
   Playwright 不支持 ubuntu26.04 runner（已加 skipif）、CI 无音频服务（已加 skipif）、
   example.com 内容改版（断言已稳健化）。详见 MAINTENANCE-LOG。
2. **Docker Build Test 失败**（待查）：首次 CI 中 Docker workflow 失败，未深入排查；
   该 workflow 与代码质量无直接关系，暂时记录不阻塞。
3. **上游 model 设置冻结在 2026-05**：新模型（如 opencode-go 的 deepseek-v4.1-flash）
   需在 `~/.aider.model.settings.yml` 里显式指定 `edit_format: diff`，否则 fallback
   到 whole 模式。本地 home 配置已处理。

## 候选池

> 筛选标准：CI 绿、带测试、改动 ≤6 文件、与优先方向（编辑质量/兼容）相关。

待每周扫描填充。首批候选（2026-11-10 确认）：
#5974 #5967 #4987 #5950 #5964 #5966（已进首批）

- **#3756**（Python 3.13 支持依赖调整）：**已评估，不采用**（2026-11-11）。上游 main 的
  requires-python 已是 >=3.10,<3.15，且 3.13/3.14 两套 venv 实测全绿，该 PR 是 2025-04
  对当时依赖的重编译，打上反而是倒退风险。
- 首次扫描发现（2026-11-11，scan-prs.sh）：**#5939**（Windows 跨盘符 common-prefix
  修复——优先方向）、**#5947**（whole 模式保留嵌套代码块——编辑质量）、**#5959**
  （--files-delta 不重发未变更文件——省 token）、#5954/#5945/#5782（Pygments 样式与
  LiteLLM 异常兼容）、#5915（忽略目录配置候选）。下批候选。
