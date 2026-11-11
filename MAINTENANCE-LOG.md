# MAINTENANCE-LOG — 行动日志

> **Fork 总目标**：上游 Aider-AI/aider 已休眠（最后提交 2026-05-22），本 fork 自用维护，
> 优先解决**编辑质量 bug** 和**兼容性**（Windows / Python 3.13-3.14）。
> 原料来源：上游 546 个 open PR（其中 475 个 CI 绿）与 1415 个 open issue 中筛选。
>
> 每条记录回答：这次试图解决什么问题 → 怎么解决的 → 验证结果如何。
> 状态性信息（基线、已知问题、候选池）见 [FORK-NOTES.md](FORK-NOTES.md)。

---

## 日志条目

<!-- 格式模板：
### YYYY-MM-DD — <一句话主题>
- **试图解决**：...
- **解决方式**：cherry-pick `889fd7f07` (PR #5974, 作者 TejasLamba2006)
- **验证结果**：pytest tests/basic 对比基线（472 绿）：
  补丁相关测试文件（test_coder.py）全绿；CI 确认后补记
-->

### 2026-11-10 — 首批补丁：写入逃逸防护（安全）

- **试图解决**：模型提议的文件路径可能 resolve 到仓库外（如 `../xxx`），模型可以借此
  读/写仓库外文件（读有内容泄露风险，写有篡改风险）
- **解决方式**：cherry-pick `889fd7f07`（PR #5974，作者 TejasLamba2006）。在
  `base_coder.allowed_to_edit`（所有 coder 写路径的必经闸门）加 `_check_path_safety`：
  用户显式添加的文件（CLI 参数、/add）信任，模型提议路径必须 resolve 在仓库内；
  editblock/patch coder 在读取前也拦截，防内容外泄。零冲突。
- **验证结果**：详见本批次末尾的整批测试记录

### 2026-11-10 — 首批补丁：editblock 缩进损坏修复

- **试图解决**：SEARCH 块整行比替换内容更短时，`match_but_for_leading_whitespace`
  的负长度切片会伪造前缀导致替换损坏
- **解决方式**：cherry-pick `8905be626`（PR #5967，作者 DZH166）。短行场景直接返回
  不匹配，杜绝负切片。零冲突。
- **验证结果**：详见本批次末尾的整批测试记录

### 2026-11-10 — 首批补丁：孤立代理字符清洗（Windows/中文崩溃修复）

- **试图解决**：Windows 中文环境下文件内容或控制台输入可能含孤立代理字符，JSON 编码
  请求时 `UnicodeEncodeError` 直接崩溃
- **解决方式**：cherry-pick `524b12905`（PR #4987，作者 graydeon）。`sanitize_for_utf8`
  递归清洗消息树，挂在 completion 调用点。零冲突。
- **验证结果**：详见本批次末尾的整批测试记录

### 2026-11-10 — 首批补丁：模型元数据缓存保护

- **试图解决**：模型元数据拉取失败时旧代码把 `"{}"` 写进缓存文件，永久毁掉本地缓存
  （之后每次启动都无法获得模型元数据，且 stale 数据也没了）
- **解决方式**：cherry-pick `49b4b5026`（PR #5950，作者 DZH166）。失败只做内存负缓存
  不写盘；`is None` 区分"未加载"和"负缓存"；空缓存文件视为缺失重新拉取。零冲突。
- **验证结果**：详见本批次末尾的整批测试记录

### 2026-11-10 — 首批补丁：配置路径 `~` 展开

- **试图解决**：`.aider.model.settings.yml` 等配置路径里的 `~` 不展开，
  HOME 目录放配置时找不到
- **解决方式**：cherry-pick `505944a2b` + `ba2478d39`（PR #5964，作者 mrhy7176，
  两个提交：fix + Windows USERPROFILE 测试修正）。零冲突。
- **验证结果**：详见本批次末尾的整批测试记录

### 2026-11-10 — 首批补丁：缺 aiohttp 的友好补救

- **试图解决**：litellm 依赖的 aiohttp 缺失时启动直接崩溃，裸装用户看到无意义的
  traceback
- **解决方式**：cherry-pick `59b7fc23a`（PR #5966，作者 Keshaw K）。缺 aiohttp 时
  交互式询问并自动安装重试，缺 litellm 给指向安装文档的报错。349 行新测试进入。
- **验证结果**：详见本批次末尾的整批测试记录

### 2026-11-11 — 修复上游竞态：用户元数据 override 被已加载的 litellm 遮蔽

- **试图解决**：首批补丁带来的 test_llm.py 暴露了一个全量测试失败
  （test_model_metadata_file，131072 != 1234）。排查确认**不是 cherry-pick 引入**，
  而是上游既有 bug：`ModelInfoManager.get_model_info` 让已加载的 litellm 优先于
  用户显式元数据（--model-metadata-file / .aider.model.metadata.json），与 main()
  的后台 litellm 加载线程存在竞态——冷启动时"碰巧"通过，热进程下 override 必然失效。
  这是真实用户可见 bug：任何自定义模型元数据在热启动后都会被 litellm 默认值覆盖。
- **解决方式**：在 `get_model_info` 开头加 local override 优先返回（本次会话修复，
  非 cherry-pick）。历史依据：测试 `ec836b99c` 的期望语义就是 override 赢，
  上游重构 `1f39c7ef0` 引入了遮蔽顺序。
- **验证结果**：组合测试（test_llm.py 13 个 + metadata 测试）14 passed；
  两套 venv 全量见整批测试记录。

### 2026-11-11 — fork CI 修复：上游休眠导致的环境漂移

- **试图解决**：fork 首次 CI 全红（Ubuntu/Windows/pre-commit/Docker 四项失败）。逐项排查
  确认**全部与首批补丁无关**，是上游休眠期间 CI 基础设施漂移：
  - `black 23.3.0`（2023 年版）在 Python 3.13+ 上崩溃（`ast.Str` 已被移除），
    连上游自己的 aider/main.py 都无法格式化
  - Playwright 1.58 不支持 GitHub 新的 ubuntu26.04 runner，浏览器装不上，
    scrape 测试连带失败
  - CI runner 无音频服务，PortAudio 初始化失败，voice 测试连带失败
  - example.com 页面内容被 IANA 改为多语言版，测试的 `"Example Domain"`
    精确断言失效
  - `test_llm.py`（#5966 带来的）import 顺序不符合 isort 规则
- **解决方式**：
  - `.pre-commit-config.yaml`：black rev 23.3.0 → 25.1.0（用 sed 精确替换）
  - `tests/basic/test_llm.py`：isort --profile black 自动修复 import 顺序
  - `tests/basic/test_voice.py`：文件级 skipif，探测 PortAudio 不可用即跳过
  - `tests/scrape/test_scrape.py`：`_playwright_ok()`（模块+chromium 探测）与
    `_playwright_module_ok()` 装饰三个依赖测试；`"Example Domain"` 断言改为
    大小写无关的 `"example"`（IANA 改版后仍稳定）
  - 第二轮 CI 暴露 Windows 3.13 上 `test_scraper_print_error_not_called`
    （外网抓取）偶发失败 → 增加 `_network_ok()` 探测，离线环境跳过；
    经排查 3.10 的失败只是 fail-fast 被 3.13 连带取消，非独立问题
  - black rev 升级引发 10 个文件重排（上游从未用新版 black 格式化过）→
    本地 `black --line-length 100 --preview .` 一次性对齐，另手工折行
    `benchmark/refactor_tools.py:173` 的超长 f-string（black 不拆字符串，
    原 `noqa` 在字符串外无效）
- **验证结果**：本地 `pytest tests/scrape tests/basic/test_voice.py
  tests/basic/test_llm.py` → 27 passed, 4 skipped（skip 均为环境探测，符合预期）；
  推送后 CI 复验（见下方 CI 记录）

### 2026-11-11 — 第二轮补丁：编辑质量 ×2、Windows 兼容 ×3、小修 ×1

- **试图解决**：继续从上游 PR 池按"编辑质量 / 兼容性"优先方向推进，覆盖上一轮未触及
  的模块（whole-file 解析、Windows 跨盘符、litellm 异常演进、legacy 控制台编码）。
- **解决方式**（全部 cherry-pick，零冲突）：
  - **#5947** `f1b20c285`（作者 ivanrxm）：whole-file 模式用嵌套深度跟踪，内容里的
    ```python/```javascript 等嵌套代码块不再被误判为文件块结束、把文件截断
  - **#5565** `45ff4a815`（作者 unknown）：`/tokens` 里两处 `"{fence}"` 漏了 f 前缀，
    估算 token 时 fence 没有被插值 → 已修
  - **#5939** `5be1c57b0`（作者 mrhy7176）：`os.path.commonpath` 跨盘符（C:\ vs D:\）
    抛的是 ValueError 而非 OSError，未捕获会崩溃 → 捕获范围补上，新增
    test_find_common_root.py
  - **#5945** `2ef6cf27d`（作者 unknown）：litellm 每次发版都可能新增异常类型，
    老代码在启动时不识别就 ValueError 崩溃 → 改为默认 warn 并继续
    （`_load(strict=False)`，CI/开发环境可开 strict 保持严格检查）
  - **#5915** `f7f71a681`（作者 mrhy7176）：`.aider.conf.yml` 候选里若混入目录路径，
    只保留真实文件（`is_file()` 过滤）
  - **#5952** `c0b87e2af` + `094b3860e`（作者 inchang-ing）：legacy Windows 控制台
    （cp1252 等）渲染模型输出时 rich 抛 UnicodeEncodeError → `_tool_message` 与
    `assistant_output` 加 ASCII 兜底重打，与第一轮 #4987（请求侧）互补
- **审查后拒绝**：
  - **#5782**：与 #5945 修同一问题（litellm 新异常），方案更粗暴（strict 才检查），
    #5945 更完整 → 二选一取 #5945
  - **#5809**：阻止系统强制英文 + `/chat-language` 命令，方向很好但**代码不完整**：
    `self.chat_language` 从未初始化（运行时 AttributeError），`user_lang` 变死变量
    → 等作者修复后再评估，记入 FORK-NOTES 候选池
- **lint 修复**：#5947 的测试代码带 trailing whitespace 与未使用变量
  （`edited_files`）→ black 25.1 重排、未使用赋值清理（仅新测试两处，精确按行号，
  避免误伤旧测试的同名变量）
- **验证结果**：flake8 7.1.0 / black 25.1 --check 全净；双 venv 全量见下。
