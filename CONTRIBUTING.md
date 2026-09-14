# 贡献指南

本文档覆盖从提 Issue 到合并 PR 的完整协作流程，并包含项目的编码规范。

---

## 目录

- [1. 环境搭建](#1-环境搭建)
- [2. 提 Issue](#2-提-issue)
- [3. Issue 拆解](#3-issue-拆解)
- [4. 认领任务](#4-认领任务)
- [5. 创建分支](#5-创建分支)
- [6. 本地开发](#6-本地开发)
- [7. 本地验证](#7-本地验证)
- [8. Commit 规范](#8-commit-规范)
- [9. 提交 PR](#9-提交-pr)
- [10. Code Review](#10-code-review)
- [11. 合并](#11-合并)
- [附录 A：编码规范](#附录-a编码规范)
- [附录 B：架构约定](#附录-b架构约定)
- [快速参考卡片](#快速参考卡片)

---

## 1. 环境搭建

```bash
# 克隆仓库
git clone https://github.com/helloworld856/social-platform-scraper.git
cd social-platform-scraper

# 创建虚拟环境（推荐）
python -m venv venv
venv\Scripts\activate       # Windows
# source venv/bin/activate  # macOS / Linux

# 安装依赖
pip install -r requirements.txt
python -m playwright install chromium

# 验证启动
python main.py
```

AIGC 功能需要额外配置 `.env`：

```env
DEEPSEEK_API_KEY=你的API Key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL_NAME=deepseek-chat
```

---

## 2. 提 Issue

所有工作从 Issue 开始。无论是 bug、功能建议还是平台爬取问题，都通过统一的 Issue 模板提交。

### 操作步骤

1. 打开 [Issues 页面](https://github.com/helloworld856/social-platform-scraper/issues)
2. 点击 **New Issue** → 选择 **「提交 Issue」**
3. 按模板填写（见下方说明）
4. 提交后等待维护者拆解和打标签

### 表单填写要点

| 字段 | 说明 |
|------|------|
| **提交前确认** | 三项 checkbox **必须全部勾选**才能提交 |
| **版本号** | 你使用的 git commit hash / tag / 分支名称 |
| **一句话概括** | 用一到两句话说清楚问题或建议 |
| **涉及工具** | 例如 `youtube_keyword_mining`；功能建议可不填 |
| **运行环境** | OS、Python 版本、浏览器版本、运行方式 |
| 🐛 问题反馈区 | 复现步骤、输入内容 |
| ✨ 功能建议区 | 使用场景、期望行为、替代方案 |
| 🌐 平台爬取区 | 涉及平台、目标链接、登录状态、账号类型、参数配置 |
| 📎 日志与截图 | 报错日志、截图、补充信息 |

> 不需要填写所有区域——根据你选的类型填写对应区域即可。

### 标题建议

- Bug：直接描述现象 — 「YouTube 关键词搜索输入 50 个关键词后闪退」
- Feature：描述期望 — 「希望 TikTok 评论支持按点赞数排序导出」
- 平台问题：标注平台 — 「X/Twitter 帖子采集无法获取浏览量」

---

## 3. Issue 拆解

> 这一步通常由**维护者**执行。如果你是来认领任务的贡献者，请跳到第 4 节。

维护者收到 Issue 后，在评论区将问题拆解为可独立交付的开发项。

### 拆解原则

- 一个 Issue 尽量对应一条干净的 commit。涉及多个独立改动时，拆分为多个子 Issue。
- 用 **checklist** 列出所有子项，让认领者清楚工作边界。

### 拆解示例

**Issue**：「YouTube 关键词搜索在输入 50 个关键词后程序闪退」

**评论区回复**：

```markdown
## 需求拆解

- [ ] 1. 排查崩溃根因：检查 `keyword_search.py` 中关键词循环是否有未捕获异常
- [ ] 2. 修复根因，确保 50+ 关键词稳定运行
- [ ] 3. 添加批量输入保护：单次关键词超过合理数量时给出警告但不阻断
```

### 标签参考

| 标签 | 用途 |
|------|------|
| `bug` | 确认为程序缺陷 |
| `enhancement` | 功能建议或改进 |
| `platform` | 特定平台的爬取/风控问题 |
| `good first issue` | 适合新贡献者入门 |
| `help wanted` | 需要社区协助 |
| `wontfix` | 不予修复，说明原因后关闭 |

---

## 4. 认领任务

在 Issue 评论区留言表明意愿，维护者会将 Issue 指派（assign）给你。

> 认领前先确认 Issue 的拆解清单已经明确，且没有被其他人认领。

---

## 5. 创建分支

```bash
git checkout main
git pull origin main
git checkout -b <分支名>
```

**分支命名规范**（小写英文 + 连字符）：

```
feat/<描述>       # 新功能    feat/tiktok-comment-sort
fix/<描述>        # Bug 修复   fix/youtube-crash-50-keywords
docs/<描述>       # 文档      docs/add-contributing-guide
refactor/<描述>   # 重构      refactor/extract-cdp-connection
```

---

## 6. 本地开发

### 必须遵守的规则

开发前请通读[附录 A：编码规范](#附录-a编码规范)。以下是**硬性要求**，违反会导致 PR 被要求修改：

| 规则 | 说明 |
|------|------|
| ✅ 中文注释 | 核心逻辑、复杂正则、多线程通信必须有中文注释 |
| ✅ 中文 Docstring | 所有核心模块的函数和类必须有中文 Docstring |
| ✅ `should_stop` | 每个采集循环迭代开头检查用户是否请求停止 |
| ✅ `wait_if_paused` | 每次核心操作前检查暂停状态 |
| ✅ `interruptible_sleep` | 用可中断等待代替 `time.sleep` |
| ✅ `random_cooldown` | 请求间隙用随机冷却模拟人类行为 |
| ✅ `build_output_path` | 输出路径统一用此函数构建 |
| ✅ manifest.json | 新增工具必须创建配套 manifest 文件 |
| ❌ 禁止 `time.sleep` | 会阻塞停止/暂停信号 |
| ❌ 禁止硬编码密钥 | API Key、密码、Cookie、Token 等绝不写入代码 |
| ❌ 禁止硬编码配置常量 | `COOLDOWN_MIN = 3.0` → 用 `config.get("cooldown_min", 3.0)` |
| ❌ 禁止拼音命名 | 变量/函数名必须用表意清晰的英文 |

### 新工具注册

新增采集工具需要三个文件：

1. **实现文件** — `src/platforms/<平台>/<工具名>.py`，包含爬虫逻辑
2. **窗口类** — 在对应平台的 `windows.py` 中注册 UI 类
3. **Manifest** — `src/platforms/<平台>/<工具名>.manifest.json`：

```json
{
  "tool_id": "youtube_keyword_mining",
  "name": "YouTube 关键词搜索",
  "category": "YouTube",
  "summary": "按关键词和日期范围搜索...",
  "entrypoint": "src.platforms.youtube.windows.YouTubeKeywordWindow",
  "implementation_path": "platforms/youtube/keyword.py",
  "tags": ["YouTube", "search", "keyword"]
}
```

---

## 7. 本地验证

提交前必须全部通过：

```bash
# 1. 静态检查（必须零 error）
ruff check .

# 2. 自动化测试
python test/test_visibility.py
python -m pytest test/test_pause_state_machine.py -v

# 3. 冒烟测试 —— 启动 GUI 手动跑一遍你的工具
python main.py
```

冒烟测试检查项：

- [ ] 工具窗口能正常打开，字段可见
- [ ] 参数配置对话框能正常修改和保存
- [ ] 启动 → 采集 → 输出 xlsx，全流程无报错
- [ ] 暂停 / 继续按钮功能正常
- [ ] 停止按钮能及时终止任务
- [ ] 输出文件字段完整，序号连续
- [ ] 异常输入（空输入、格式错误）不会崩溃

---

## 8. Commit 规范

```bash
git add <文件>
git commit -m "<type>: <简短描述>"
```

### 格式

```
<type>: <中文简述>

原逻辑：<改之前的行为>
新逻辑：<改之后的行为>
```

如果改动不是修复/重构，正文可省略。Commit 类型：

| 类型 | 用途 |
|------|------|
| `feat` | 新功能 |
| `fix` | Bug 修复 |
| `docs` | 文档更新 |
| `refactor` | 重构（不改变功能） |
| `chore` | 构建、CI、依赖等杂务 |

### 示例

```
feat: 添加 TikTok 评论按点赞数排序导出

原逻辑：评论按抓取顺序导出，不排序。
新逻辑：增加 sort_by_likes 参数，开启后按点赞数降序排列，默认关闭保持兼容。
```

```
fix: 修复 YouTube 关键词搜索超过 50 个时崩溃

原逻辑：search_all_keywords 中未捕获单关键词失败，Queue.get 超时后直接 raise。
新逻辑：单关键词异常时 log 错误并 continue，Queue.get 加 try/except 兜底。
```

---

## 9. 提交 PR

### 操作

```bash
git push origin <分支名>
```

在 GitHub 上点击 **Compare & pull request**，确认 base 为 `main`，按 PR 模板填写。

### PR 模板各项说明

| 区域 | 要求 |
|------|------|
| **变更说明** | 简述这个 PR 做了什么、为什么要做 |
| **关联 Issue 与需求拆解** | 用 `Closes #12` 链接 Issue；列出「已修复/实现」和「未修复/计划后续」的拆解项 |
| **测试** | 勾选 lint、自动化测试、手动测试（注明平台和工具） |
| **检查清单** | 全部勾选后才请求 review |
| **截图** | UI 改动附前后对比，非 UI 改动可省略 |
| **注意事项** | 破坏性变更、迁移步骤等 reviewer 需要注意的点 |

### PR 标题

保持与 Issue 的关联：

```
fix: 修复 YouTube 关键词搜索崩溃 (fixes #12)
feat: TikTok 评论按点赞数排序导出 (closes #34)
```

---

## 10. Code Review

### 提交者侧

1. PR 创建后，在右侧 **Reviewers** 添加维护者
2. 收到 review 意见后，在本地修改 → `git push`，PR 自动更新
3. **逐条回复每个 comment**：已修改的说明修改方式，不修改的说明理由
4. 所有 conversation 解决后，请求重新 review

### 审查者侧

审查时关注以下维度：

| 维度 | 检查点 |
|------|--------|
| **正确性** | 逻辑是否正确？边界条件（空输入、网络超时、风控拦截）是否处理？ |
| **安全性** | 是否有密钥泄露？输入是否有注入风险？ |
| **协作机制** | 循环中是否调用 `should_stop` / `wait_if_paused`？是否用 `interruptible_sleep` 代替 `time.sleep`？ |
| **一致性** | 命名、注释密度、代码风格是否与项目一致？是否用 `build_output_path` 构建路径？ |
| **可维护性** | 核心逻辑是否有中文注释？新增工具是否创建 manifest.json？ |
| **变更范围** | 是否存在不相关的格式化改动？diff 是否聚焦？ |

审查通过后点击 **Approve**。

---

## 11. 合并

合并条件：

- [ ] 所有 CI 检查通过（ruff + test_visibility）
- [ ] 至少一位维护者 Approve
- [ ] 所有 review conversation 已解决

合并方式：**Squash and merge**，将分支上的多个 commit 压缩为一条干净的 commit。

合并后删除远程分支（GitHub 会自动提示）。

合并 commit 格式参考：

```
feat: TikTok 评论按点赞数排序导出 (#34)

- 增加 sort_by_likes 配置项，默认关闭保持兼容
- 按点赞数降序排列输出行
- 更新参数配置对话框增加对应开关
```

---

## 附录 A：编码规范

### 通用规范

- **语言**：Python 3.13
- **Lint**：`ruff`，配置在 `pyproject.toml`
  - `line-length = 150`
  - 忽略 `E402`（模块级导入不在文件顶）、`F841`（未使用的局部变量）
- **命名**：遵循 PEP 8，变量和函数名用表意清晰的英文，**禁止拼音或无意义缩写**
- **注释**：
  - 核心逻辑、复杂正则、多线程通信机制 → **必须**有清晰中文注释
  - 所有核心模块的函数和类 → **必须**有中文 Docstring
- **Commit**：格式 `<type>: <中文简述>`，修复类建议注明「原逻辑」和「新逻辑」

### 爬虫核心 API

在 `src/platforms/` 下新增或修改爬虫脚本时，必须使用 `src.core` 中的公共组件：

```python
from src.core.timing import should_stop, wait_if_paused, interruptible_sleep, random_cooldown
from src.core.output import build_output_path
```

**任务状态控制（必须）**：

```python
def run_task(values, log_callback, finish_callback, stop_event, pause_event=None):
    for item in items:
        # 每个循环迭代开头检查停止信号
        if should_stop(stop_event):
            break

        # 每次核心操作前检查暂停状态
        wait_if_paused(pause_event, stop_event)

        # 采集逻辑 ...

        # 可中断等待（代替 time.sleep）
        interruptible_sleep(2.0, stop_event, pause_event)

        # 随机冷却（模拟人类行为，防止风控）
        random_cooldown(3.0, 8.0, stop_event, pause_event)
```

- **严禁** `time.sleep` — 会阻塞线程导致无法响应停止/暂停
- **必须** 使用 `interruptible_sleep(duration, stop_event)` 做可中断延时
- **建议** 使用 `random_cooldown(min_sec, max_sec, stop_event, pause_event)` 在请求间隙模拟随机停顿

**YouTube API 调用（建议）**：

```python
# 在实现文件中定义 retry wrapper，包裹 .execute()
def _api_execute_with_retry(request, log_callback=None, stop_event=None, max_retries=3):
    from googleapiclient.errors import HttpError
    for attempt in range(1, max_retries + 1):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status in (500, 503, 429):
                if attempt < max_retries and not (stop_event and stop_event.is_set()):
                    interruptible_sleep(2 ** attempt, stop_event)
                    continue
            raise
    return request.execute()

response = _api_execute_with_retry(youtube.search().list(**params), log_callback, stop_event)
```

- 禁止直接 `.execute()` 而不加重试——一个瞬态 500 会丢掉整页数据。
- 优先用 `playlistItems.list`（频道上传列表）而非 `search.list`：前者是确定性接口，不受搜索索引时间衰减影响。

**输出路径（必须）**：

```python
path = build_output_path("youtube", "keyword_search_20260604.xlsx")
# → output/youtube/keyword_search_20260604.xlsx
```

禁止硬编码绝对路径或手动拼接路径。

**敏感信息（绝对禁止）**：

- 不得在代码中硬编码 API Key、密码、Cookie、Token
- `.env` 文件不得提交到仓库（已在 `.gitignore` 中）

### 输入文件规范

```
# 注释行，会跳过
# 空行也会跳过

https://www.youtube.com/@example1
https://www.youtube.com/@example2
```

- 每行一条记录
- `#` 开头 → 注释，跳过
- 空行 → 跳过
- 多字段 → 空格或制表符分隔

---

## 附录 B：架构约定

### 工具窗口结构

每个工具是一个 `SimpleToolWindow` 子类：

```python
class YouTubeKeywordWindow(SimpleToolWindow):
    tool_id = "youtube_keyword_mining"

    def field_specs(self):
        return [
            FieldSpec("api_key", "Google API Key", "text"),
            FieldSpec("keywords_file", "关键词文件", "text_or_file"),
            # ...
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event=None):
        # values: 来自 UI 字段 + 参数配置的合并字典
        config = {k: v for k, v in values.items() if k in RELEVANT_CONFIG_KEYS}
        run_keyword_spider(values, config, log_callback, finish_callback, stop_event, pause_event)
```

### 配置系统

工具参数分两层：

**全局配置**（主窗口「全局配置」按钮）— 9 个跨工具共享参数：
`page_load_timeout`、`scroll_interval`、`no_new_scroll_limit`、`max_scrolls`、`scroll_px`、`cooldown_min`、`cooldown_max`、`save_batch_size`、`comment_top_limit`。修改后所有工具默认继承，工具可单独覆盖。

**工具特有参数**（工具窗口「参数配置」按钮）：

```python
def tool_config_params(self):
    return [
        ConfigParam("max_results", "最多搜索结果数", kind="int", default=5000, minimum=1, maximum=999999),
        ConfigParam("page_ready_wait", "页面就绪等待(秒)", kind="float", default=2.5, minimum=0.5, maximum=15.0, step=0.1, decimals=1),
    ]
```

- `ConfigParam` 定义的参数自动渲染为配置对话框表单
- 用户修改后自动持久化到 `config/{tool_id}.json`
- **合并优先级**：工具 JSON > 全局 JSON > 工具默认值
- **别名映射**：工具使用非标准参数名时（如 `youtube_browser_page_timeout`），通过 `GLOBAL_ALIAS_MAP` 桥接到全局标准名
- `_run_worker` 自动将 `config_values` 合并到 `values` 再传给 `run_task`
- 每个 `ConfigParam` 必须同时加入 `DEFAULT_CONFIGS[tool_id]` 字典

### 线程模型

- `run_task` 在 `threading.Thread`（非 daemon）中执行
- UI 更新通过 `WorkerSignals` 的 pyqtSignal 实现线程安全
- 采集循环通过 `stop_event` / `pause_event` 与 UI 按钮协作

---

## 快速参考卡片

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 完整流程

 提 Issue → 拆解 → 认领 → 切分支 → 开发 → 验证 → 提 PR → Review → 合并
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

 分支：feat/xxx   fix/xxx   docs/xxx   refactor/xxx
 Commit：feat: 中文简述
 PR 标题：fix: 中文简述 (fixes #12)
 合并：Squash and merge

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 提交前必过

 ruff check .           ← 零 error
 python test/test_visibility.py
 pytest -v
 python main.py         ← 冒烟测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

 爬虫铁律

 ❌ 禁用 time.sleep
 ❌ 禁用硬编码密钥
 ✅ 用 should_stop / wait_if_paused
 ✅ 用 interruptible_sleep / random_cooldown
 ✅ 用 build_output_path
 ✅ 写中文注释
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```
