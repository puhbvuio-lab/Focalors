# 社媒平台数据爬取工具

一个 PyQt 桌面工具台，用于集中启动 YouTube、TikTok、X/Twitter、Steam、Instagram、Facebook 六个平台的数据采集工具，并提供 AIGC 标题判断、关键词 XLSX 合并等数据处理功能。

## 环境要求

- Python 3.13（唯一支持版本）。
- Windows + Chrome 或 Chromium。
- TikTok 和 X/Twitter 工具依赖 Playwright 接管浏览器。
- YouTube 工具需要 Google API Key。
- Steam 工具主要调用 Steam 公共接口；玩家画像补采、IStoreService AppList 和部分增强能力需要 Steam Web API Key。
- AIGC 判断工具需要 DeepSeek 兼容接口配置。

## 安装和启动

首次运行先安装依赖和 Playwright 浏览器：

```bash
pip install -r requirements.txt
python -m playwright install chromium
python main.py
```

之后启动只需要：

```bash
python main.py
```

各平台（TikTok、X/Twitter、Instagram）工具会自动使用项目根目录下的 `user_data/` 启动 Chrome 调试浏览器。首次使用时，需要在自动打开的浏览器里登录对应平台。登录态会保存在 `user_data/`，后续通常不用重复登录。

## AIGC 配置

AIGC 判断工具需要提前配置 `.env`。推荐在项目根目录创建 `.env`：

```env
DEEPSEEK_API_KEY=你的 DeepSeek API Key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL_NAME=deepseek-chat
```

同时兼容旧变量名：`API_KEY`、`BASE_URL`、`MODEL_NAME`。

不要把真实 API Key 提交到代码仓库。

## 目录结构

- `main.py`：桌面工具台入口。
- `requirements.txt`：运行依赖。
- `src/studio/`：PyQt 主工具台、组件发现、独立工具进程启动器。
  - `discovery.py`：动态扫描 `manifest.json` 发现工具组件。
  - `registry.py`：工具注册表（调用 discovery 动态生成）。
  - `qt_app.py`：主窗口，支持热重载工具。
  - `tool_runner.py`：独立进程启动器。
- `src/ui/`：工具窗口公共基类。
- `src/core/`：输出路径、XLSX 写入、数字转换、文本清洗、Chrome CDP、等待机制等公共能力。
- `src/platforms/youtube/`：YouTube 采集工具。
- `src/platforms/tiktok/`：TikTok 采集工具。
- `src/platforms/x_twitter/`：X/Twitter 采集工具。
- `src/platforms/steam/`：Steam API 游戏数据采集与评论玩家画像补采工具。
- `src/platforms/instagram/`：Instagram 采集工具。
- `src/platforms/facebook/`：Facebook 采集工具。
- `src/judge_aigc/`：AIGC 判断引擎（LangGraph + DeepSeek）。
- `src/processing/`：AIGC 判断入口、关键词 XLSX 合并。
- `src/ui/config_dialog.py`：各工具的参数配置对话框。
- `test/`：UI 逻辑测试和暂停功能测试。
- `user_data/`：各平台浏览器登录态目录。
- `output/`：默认输出目录，按平台分目录存放。`output/temp/` 存放文本输入中转文件的临时目录。

## 组件化架构

工具采用组件化架构，每个工具由两个文件组成：

- **实现文件**：如 `keyword.py`，包含爬虫逻辑。
- **manifest 文件**：如 `keyword.manifest.json`，描述工具元数据（名称、分类、标签等）。

manifest 文件格式：

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

### 热重载

主窗口提供「重载工具」按钮，用于：

- **新增工具**：创建 manifest 和实现文件后，点击重载即可发现新工具。
- **修改元数据**：更新 manifest 中的名称、描述等，点击重载刷新工具列表。
- **修改代码**：工具作为独立子进程运行，每次启动自动加载最新代码，无需重载。

工具进程隔离：每个工具在独立进程中运行，单个工具崩溃不影响主窗口和其他工具。

### 添加新工具

在已有平台下添加（以 YouTube 为例）：

1. 创建实现文件：`src/platforms/youtube/new_tool.py`
2. 在 `windows.py` 中添加窗口类
3. 创建 manifest：`src/platforms/youtube/new_tool.manifest.json`
4. 点击主窗口「重载工具」

## 通用输入规则

- TXT 输入文件默认每行一条记录。
- 空行会跳过。
- 以 `#` 开头的行会跳过。
- 链接可以带参数，程序会尽量清理为标准链接。
- 多字段 TXT 通常用空格或制表符分隔。
- 大部分文本输入框支持「直接输入」和「选择 TXT 文件」两种模式，可通过下拉框切换。
- 输出文件默认是 `.xlsx`，写入 `output/` 下对应平台目录。
- 长任务通常会分批保存，减少中途失败造成的数据损失。
- 每个工具窗口的「参数配置」按钮可调整该工具特有的爬取行为参数，修改后的参数会保留到下次打开。
- 主窗口右上角的「全局配置」按钮可调整所有工具**共享**的参数（页面加载超时、滚动间隔、冷却等待、保存频率等 9 项）。工具特有参数会覆盖全局值。例如：全局设 `page_load_timeout=60000`，Facebook 工具可单独设 `90000`；未单独配置的工具自动继承全局值。

## YouTube 工具

### YouTube 关键词视频搜索

用途：按关键词和日期范围搜索 YouTube 视频，导出基础指标，并可选补充长短视频类型与快照任务。

输入：

- `Google API Key`
- 是否限制时间
- 开始日期：`YYYY-MM-DD`
- 结束日期：`YYYY-MM-DD`
- 关键词：每行一个
- 是否获取视频评论信息
- 最多获取评论数
- 是否检测长短视频类型
- 自动生成3日快照
- 自动生成7日快照

可通过「参数配置」调整：每个关键词最多视频数（默认 5000）、搜索方式（浏览器优先可节省 99% API 配额）、搜索每页条数、日期切分粒度、视频详情每批条数、浏览器滚动相关参数、每个视频评论最多输出条数。

输出字段包括：标题、视频类型、时长、播放量、点赞数、评论数、作者粉丝数、视频简介、发布时间、视频链接、作者主页链接。

### YouTube 作者信息提取

用途：从作者主页链接 TXT 中批量提取频道资料。

TXT 格式：

```txt
https://www.youtube.com/@example
https://www.youtube.com/channel/UCxxxx
```

输出字段包括：

- 作者主页链接
- 作者名称
- 作者 ID
- 粉丝量
- 作者简介

### YouTube 目标视频前后指标

用途：读取目标视频和博主主页，定位目标视频，并导出目标前后各 5 条视频指标。

TXT 格式：

```txt
视频链接 博主主页链接
https://www.youtube.com/watch?v=xxxx https://www.youtube.com/@example
```

输入：

- `Google API Key`
- 视频链接 + 博主主页：每行一对

可通过「参数配置」调整：目标视频前后各取几条（默认 5）、上传列表最多翻页数（默认 200）。

### YouTube 作者主页作品采集

用途：输入作者主页链接，采集主页下 `Videos`、`Shorts`、`Posts` 的公开作品。

实现方式：

- `Videos` / `Shorts`：优先使用 YouTube Data API 读取频道公开视频上传列表和视频统计；API 不可用、超时或未返回结果时，会尝试用 Playwright 接管本地 Chrome 读取 `/videos` 和 `/shorts` 页面。
- `Posts`：使用 Playwright 打开作者主页 `/posts` 页面采集社区帖文本和图片占位。

输入：

- `Google API Key`
- 作者主页链接：每行一个
- 是否限制时间
- 开始日期：`YYYY-MM-DD`
- 结束日期：`YYYY-MM-DD`
- 是否获取视频评论信息
- 最多获取评论数

可通过「参数配置」调整：每个作者最多视频/Shorts 数（默认 5000）、页面加载超时、滚动间隔、初始加载等待、无新内容停止阈值、每次滚动像素、Posts 最大滚动次数（默认 200）、每批保存条数。

示例：

```txt
https://www.youtube.com/@example
https://www.youtube.com/channel/UCxxxx
```

输出字段：序号、视频链接、博主主页链接、作者主页链接、标题、作品内容、频道名称、发布日期、视频类型、视频时长、视频简介、播放量、点赞数、评论数。

规则：

- `Videos` 和 `Shorts` 输出标题后追加 `[视频]`；`Posts` 保留完整文本 + `[图片]`。
- 视频类优先使用 API（`playlistItems.list` + `videos.list`），可精确过滤发布日期。API 不可用时自动降级为浏览器滚动采集，并通过相对时间文本（如 "3 days ago"）做近似日期过滤。
- 帖子统一使用浏览器采集；若开启评论且 API 可用，视频/Shorts 通过 API 获取评论，帖子通过浏览器 DOM 抓取评论。

### YouTube 视频高赞主楼评论

用途：读取视频链接 TXT，导出每个视频点赞量最高的前 100 条主楼评论。

TXT 格式：

```txt
https://www.youtube.com/watch?v=xxxx
https://youtu.be/yyyy
```

输入：

- `Google API Key`
- 视频链接：每行一个

可通过「参数配置」调整：每个视频最多扫描主楼评论数（默认 500）、每个视频评论最多输出条数、API 评论每页条数。

## Steam 工具

Steam 工具位于 `src/platforms/steam/`，不依赖 Playwright 或浏览器登录态，主体逻辑通过 `requests` 调用 Steam 商店公开接口和 Steam Web API。当前有两个工具：

- `steam_api_research`：Steam API 游戏数据采集。
- `steam_player_profiles`：Steam 评论玩家画像补采。

输出统一写入 `output/steam/` 下的渠道目录，并使用 checkpoint 多 sheet 写入器保存进度。任务中断或再次运行相同范围时，会跳过已完成项。

### Steam API 游戏数据采集

用途：按 Steam AppID、商店链接或关键词采集游戏商店信息，并可选补充玩家评论、新闻、当前在线人数、成就数量和评论玩家画像。

输入：

- `Steam Web API Key`：可选。普通商店详情、公开评论、新闻和当前在线人数可不填；IStoreService AppList 关键词补漏、评论玩家画像增强等能力需要填写。
- AppID / 商店链接：每行一个，例如 `1623730` 或 `https://store.steampowered.com/app/1623730/Palworld/`。
- 关键词：每行一个。AppID/商店链接和关键词至少需要提供一种。
- 商店语言：默认 `english`，可选 `schinese`、`japanese`、`tchinese` 等。
- 商店地区代码：默认 `US`，填写两位国家/地区代码，例如 `JP`、`CN`。
- 是否采集玩家评论/评价摘要。
- 每个游戏最多玩家评论数：`0` 表示只读取评价摘要，不导出评论明细。
- 是否采集新闻。
- 每个游戏最多新闻数。

运行逻辑：

1. 先解析直接输入的 AppID 和商店链接，并对关键词做去重。
2. 对关键词按配置发现候选 App：
   - `商店搜索接口（推荐）`：调用 Steam Store Search 接口分页搜索，速度最快。
   - `商店搜索后补 AppList`：先走商店搜索，不足数量时再用 AppList 本地匹配补漏。
   - `AppList 本地匹配`：加载完整 AppList 后按名称做本地包含匹配，首次较慢但适合补漏。
3. AppList 会缓存到本地应用状态目录的 `steam_cache`，缓存有效期由 `cache_ttl_hours` 控制。
4. 合并直接输入和关键词发现结果，按 `AppID + 来源` 去重。
5. 使用 `parallel_workers` 并发采集每个 App：
   - `appdetails`：商店详情、价格、发行信息、开发商/发行商、标签、配置、截图、视频、官网等。
   - `appreviews`：评价摘要和可选评论明细。
   - `ISteamNews/GetNewsForApp`：可选新闻。
   - `GetNumberOfCurrentPlayers`：可选当前在线人数，默认开启。
   - `GetGlobalAchievementPercentagesForApp`：可选成就数量和样本。
6. 默认跳过非 `game` 类型 App；如需保留 DLC、软件等，可在参数配置里开启“是否保留非游戏 App”。
7. 如果开启“评论玩家画像增强”，会从评论里的 SteamID 继续补采玩家公开资料、游戏库、最近游玩、目标游戏成就、徽章和好友；该增强需要 Steam Web API Key。

可通过「参数配置」调整：

- API 并发数（1\~8，默认 1）。
- 每个关键词最多游戏数（默认 100）。
- 关键词发现方式。
- 是否保留非游戏 App。
- 是否采集当前在线人数。
- 是否采集成就数量。
- 评论玩家画像增强、每个游戏最多增强评论玩家数。
- 玩家画像增强项：公开游戏库、最近游玩、目标游戏成就、徽章、公开好友。
- 评论语言、评论排序/过滤。
- API 请求超时、单请求间隔、关键词/AppList 缓存有效期、每批保存条数。

输出文件示例：

```txt
output/steam/api_research/20260702/steam_api_research_20260702_153000.xlsx
```

输出 sheet：

- `游戏信息`：AppID、游戏名、类型、价格、发行日期、开发商、发行商、标签、平台、配置、评价摘要、当前在线、成就数量、素材链接、商店链接等。
- `玩家评论`：评论ID、SteamID、是否推荐、语言、购买类型、游玩小时、发布时间、评论链接、评论内容等。
- `新闻`：新闻ID、标题、作者、发布时间、来源、链接、内容摘要等。
- 开启评论玩家画像增强时，还会写入 `玩家画像`、`玩家游戏库`、`最近游玩`、`目标游戏成就`、`玩家徽章`、`玩家好友`。

注意：

- Steam 玩家资料、游戏库、好友、成就等信息受用户隐私设置影响；私密或接口不可见时，会在 `隐私/错误` 字段记录原因。
- 不要把真实 Steam Web API Key 写进仓库配置或 README。

### Steam 评论玩家画像补采

用途：从已有 Steam 玩家评论 Excel 或手填 SteamID/玩家主页链接中提取玩家，补采玩家公开画像，用于构建游戏玩家分析表。

输入：

- `Steam Web API Key`：必填。
- 已有玩家评论 Excel：可选，通常选择 “Steam API 游戏数据采集” 产出的 `.xlsx`。
- SteamID / 主页链接：可选，每行一个，例如 `7656119...` 或 `https://steamcommunity.com/profiles/7656119...`。
- 目标游戏 AppID / 商店链接：手填 SteamID 时可选，用于补采该游戏下的玩家成就。
- 目标游戏名：手填 SteamID 时可选。
- 成就语言：默认 `english`。

运行逻辑：

1. 如果选择 Excel，优先读取 `玩家评论` sheet；没有该 sheet 时读取第一个 sheet。
2. 从行数据里提取 SteamID，并尽量继承 AppID、游戏名、是否推荐、目标游戏游玩小时、评价时游玩小时、最后游玩时间等上下文。
3. 如果手填 SteamID 或玩家主页链接，会解析 64 位 SteamID，并和 Excel 来源合并。
4. 按 `目标AppID + SteamID` 去重，并受 `max_player_profiles` 限制。
5. 先按 100 个 SteamID 一批读取玩家公开资料和封禁信息。
6. 再按 `player_parallel_workers` 并发补采每个玩家：
   - Steam 等级。
   - 公开游戏库。
   - 最近游玩。
   - 玩家徽章。
   - 公开好友。
   - 目标游戏玩家成就（需要有目标 AppID）。
7. 每个玩家完成后立即写入对应 sheet，并通过 checkpoint 标记完成。

可通过「参数配置」调整：

- 最多增强玩家数（默认 2000）。
- 玩家画像 API 并发数（1\~8，默认 2）。
- 是否采集公开游戏库。
- 每玩家最多游戏库条数，`0` 表示全部。
- 是否采集最近游玩、目标游戏成就、徽章、公开好友。
- API 请求超时、单请求间隔、每批保存条数。

输出文件示例：

```txt
output/steam/player_profiles/20260702/steam_player_profiles_20260702_153000.xlsx
```

输出 sheet：

- `玩家画像`：SteamID、昵称、主页链接、头像、地区、在线状态、账号创建时间、目标游戏推荐/游玩信息、Steam等级、徽章数、公开好友数、公开游戏数、封禁信息、隐私/错误等。
- `玩家游戏库`：库游戏 AppID、游戏名、总游玩小时、近两周游玩小时、最后游玩时间等。
- `最近游玩`：最近游戏、近两周游玩小时、总游玩小时。
- `目标游戏成就`：成就 API 名、成就名称、描述、是否解锁、解锁时间。
- `玩家徽章`：徽章ID、等级、XP、稀有度、完成时间、关联 AppID。
- `玩家好友`：好友 SteamID、关系、加好友时间。

## TikTok 工具

TikTok 工具需要先登录自动打开的浏览器。若页面打不开、评论不可见或加载异常，先确认浏览器登录态和网络环境。

### TikTok 关键词视频基础信息

用途：按关键词搜索 TikTok，并按日期范围过滤视频发布时间。

输入：

- 是否限制时间
- 开始日期：`YYYY-MM-DD`
- 结束日期：`YYYY-MM-DD`
- 关键词：每行一个
- 是否获取视频评论信息
- 最多获取评论数

可通过「参数配置」调整：关键词爬取并行 tab 数（1\~3，默认 1）、评论爬取并行 tab 数（1\~3，默认 1）、评论队列最大长度、每个关键词最多视频数（默认 1000）、每个关键词最多检查候选数（默认 3000）、搜索滚动间隔范围、搜索页刷新重试次数、最大搜索滚动次数、无新内容停止阈值、每个视频评论最多输出条数。

输出字段包括：

- 视频标题
- 播放量
- 点赞数
- 收藏量
- 评论数
- 发布时间
- 视频链接
- 作者信息

### TikTok 博主信息提取

用途：从博主主页 TXT 批量提取博主资料。

TXT 格式：

```txt
https://www.tiktok.com/@username
```

输出字段包括：

- 博主主页链接
- 博主名称
- 博主 ID
- 粉丝量
- 作者简介

### TikTok 博主主页视频指标采集

用途：输入博主主页链接，滚动采集主页公开视频列表，并按日期范围过滤和获取视频互动指标。

TXT 格式：

```txt
https://www.tiktok.com/@username
```

输入：

- 博主主页链接：每行一个
- 是否限制时间
- 开始日期：`YYYY-MM-DD`
- 结束日期：`YYYY-MM-DD`
- 是否获取视频评论信息
- 最多获取评论数

可通过「参数配置」调整：页面加载超时、滚动间隔、无新内容停止阈值、最大滚动次数（默认 200）、每批处理视频数、每 N 条保存一次、批量随机等待范围。

输出字段包括：

- 视频标题
- 播放量
- 点赞数
- 评论数
- 分享数
- 发布时间
- 视频链接

### TikTok 视频链接采集

用途：直接输入视频链接，采集指定视频的简介、发布时间和互动指标，不需要先输入博主主页。

输入：每行一个标准视频链接或 TikTok 分享短链。

```txt
https://www.tiktok.com/@username/video/1234567890
https://vm.tiktok.com/xxxx/
```

输出字段：序号、视频链接、发布日期、视频简介、点赞数、评论数、收藏量、分享数、采集状态、失败原因。

说明：短链会在已登录的 Chrome 中展开为标准视频链接。需要采集评论明细时，使用「TikTok 视频高赞主楼评论」工具。

### TikTok 目标视频前后指标

用途：读取目标视频和博主主页，在博主主页定位目标视频，并导出目标前后视频指标。

TXT 格式：

```txt
视频链接 博主主页链接
https://www.tiktok.com/@user/video/123 https://www.tiktok.com/@user
```

输入：

- 视频链接 + 博主主页：每行一对

可通过「参数配置」调整：目标视频前后各取几条（默认 5）、API 每页条数、API 最大翻页数、主页最大滚动次数、主页滚动间隔。

### TikTok 视频高赞主楼评论

用途：读取视频链接 TXT，抓取每个视频的主楼评论，并导出点赞量最高的评论。

TXT 格式：

```txt
https://www.tiktok.com/@user/video/123
```

输入：

- 视频链接：每行一个
- 每个视频最多扫描主楼评论数：默认 500

可通过「参数配置」调整：每个视频评论最多输出条数、页面加载超时、评论滚动间隔、最大滚动轮数。

规则：

- 只保留主楼评论。
- 二级回复不作为主楼评论写入。
- emoji 和文本会保留。
- 非文本内容会用类似 `[图片]` 的占位写入。
- 每爬完一个视频就保存一次。

## Instagram 工具

Instagram 工具使用 Playwright 接管本地 Chrome。运行前建议先在自动打开的 Chrome 中登录 Instagram；未登录、账号受限、主页私密或页面被风控时，采集结果会受影响。

### Instagram 作者主页作品采集

用途：输入作者主页链接，滚动采集主页下公开作品，并逐条打开详情页读取发布时间、文本内容和页面可识别指标。

输入：

- 作者主页链接：每行一个

可通过「参数配置」调整：每个作者最多作品数（默认 5000）、页面加载超时、滚动间隔、每次滚动像素、无新内容停止阈值、每个主页最大滚动次数（默认 200）、每 N 条保存一次、批量随机等待范围。

脚本内置：每采集并写入 10 条作品保存一次，并随机等待 10 到 25 秒，避免访问过快。

示例：

```txt
https://www.instagram.com/username/
```

输出字段：

- 序号
- 作品ID
- 作品链接
- 发布时间
- 作品内容
- 浏览量
- 评论数
- 点赞数

规则：

- Reels 或视频作品只取标题/首行文本，并追加 `[视频]`。
- 图片作品会尽量保留完整文本，并追加 `[图片]`。
- 轮播作品按页面可识别媒体类型追加占位。
- 每爬完一个作者主页就保存一次。

## X/Twitter 工具

X/Twitter 工具需要先登录自动打开的浏览器。若没有登录或账号被风控，页面 DOM 可能加载不完整，采集结果会受影响。

### X 关键词媒体推文搜索

用途：按关键词和日期范围搜索 X 推文，导出含视频或图片的原创媒体推文。

输入：

- 关键词：每行一个
- 目标语言：不限、中文、英文、日文、韩文、俄文、西语、法语、德语
- 是否限制时间
- 开始日期：`YYYY-MM-DD`
- 结束日期：`YYYY-MM-DD`
- 是否获取推文评论信息
- 最多获取评论数

可通过「参数配置」调整：关键词爬取并行 tab 数（1\~3，默认 1）、评论爬取并行 tab 数（1\~3，默认 1）、评论队列最大长度、评论间隔等待范围、切片跨度（默认 7 天）、搜索页加载超时、滚动等待范围、搜索页刷新重试次数、无新内容停止阈值、每个时间切片最大滚动次数（默认 200）。

说明：

- 会跳过转推。
- 会跳过引用或嵌套推文。
- 主要保留含图片或视频的原创推文。

### X 推文作者资料提取

用途：读取推文链接或博主链接 TXT，提取推文作者资料。

TXT 格式：

```txt
https://x.com/user/status/123
https://twitter.com/user/status/456
```

输入：

- 输入方式：推文链接 或 博主链接
- 链接列表：每行一个

可通过「参数配置」调整：页面加载超时、推文渲染等待时间。

输出字段包括：

- 推文链接
- 作者主页
- 作者名称
- 账号 ID
- 粉丝数

### X 目标推文前后指标

用途：读取目标推文和博主主页，导出目标推文前后各 5 条推文指标。

TXT 格式：

```txt
推文链接 博主主页链接
https://x.com/user/status/123 https://x.com/user
```

输入：

- 推文链接 + 博主主页：每行一对

可通过「参数配置」调整：目标推文前后各取几条（默认 5）、主页最大滚动次数、主页滚动间隔、页面加载超时。

说明：

- 中间可以用空格或制表符分隔。
- 如果目标推文链接能识别作者，会优先用链接里的作者信息辅助定位。

### X 指定推文指标采集

用途：读取推文链接 TXT，逐条打开推文详情页，采集指定推文的内容和互动指标。

TXT 格式：

```txt
https://x.com/user/status/123
https://x.com/user/status/456
```

输入：

- 推文链接：每行一个
- 是否获取推文评论信息
- 最多获取评论数

可通过「参数配置」调整：页面就绪等待（goto 后缓冲 React 渲染）、评论首屏渲染等待、评论滚动最小/最大间隔、评论无新增停止阈值、推荐区后继续扫描轮数、最多输出评论数、冷却间隔条数、并发 Tab 数。

输出字段：序号、推文链接、推文的内容、浏览量、评论数、点赞数、转发量、标签、推文内的图片链接、推文内的视频链接。

规则：

- 页面加载后自动检测登录页，失败时打印当前 URL 便于排查。
- 单条读取失败会自动重建标签页重试一次，仍失败才写入空指标行，避免因标签页被关闭或页面卡死产生空数据。
- `推文内的图片链接`、`推文内的视频链接` 按行分隔写在同一个单元格内（Excel 已开启自动换行）。图片优先取原图地址，视频取最高分辨率 mp4；引用推文的视频会回退到被引用推文解析。
- 正文里被排版拆断的外链（`https://` 与域名被换行分开）会自动还原成完整地址。
- 评论扫描会先等待首屏回复渲染稳定，出现「发现更多」推荐区后仍继续滚动，只有连续多轮无新增评论才停止；推荐区下方的推荐推文不会被当成评论写入。
- 非文本内容会用类似 `[图片]`、`[视频]`、`[GIF]`、`[卡片]` 的占位写入。
- 标签编码：`0`=图片+视频，`1`=图片，`2`=视频，`3`=纯文本，`4`=其它。
- 冷却间隔、冷却等待时长均可通过 UI 配置（全局参数可统一调整）。

### X 博主主页帖子采集

用途：输入博主主页链接，滚动采集该博主主页公开展示的帖子。

输入：

- 博主主页链接：每行一个
- 是否限制时间
- 开始日期：`YYYY-MM-DD`
- 结束日期：`YYYY-MM-DD`
- 是否获取推文评论信息
- 最多获取评论数

可通过「参数配置」调整：页面加载超时、滚动间隔、无新内容停止阈值、每个主页最大滚动次数（默认 200）、每 N 条保存一次、批量随机等待范围。

示例：

```txt
https://x.com/username
https://twitter.com/another_user
```

输出字段：

- 序号
- 帖子 ID
- 发布时间
- 帖子内容
- 帖子链接

说明：

- 只保留链接作者本人发布的帖子。
- 会跳过广告标记。
- 无文本但有媒体内容时，会写 `[图片]`、`[视频]`、`[GIF]`、`[卡片]`。
- 每采集并写入 10 条帖子会保存一次，并随机等待 6 到 15 秒。
- 脚本会控制滚动和详情读取节奏；如果网络慢，可适当增大最大滚动次数后重跑。

### X 推文高赞主楼评论

用途：读取推文链接 TXT，扫描主楼评论，并导出点赞量最高的前 100 条评论。

TXT 格式：

```txt
https://x.com/user/status/123
```

输入：

- 推文链接：每行一个
- 每条推文最多扫描主楼评论数：默认 500

可通过「参数配置」调整：每条推文评论最多输出条数、页面加载超时、评论滚动间隔、无新内容停止阈值。

规则：

- 主推文本身不会作为评论保存。
- 只保存直接回复主推文的一级评论。
- 不保存二级回复、三级回复。
- 跳过广告或推广推文。
- 遇到 `Discover more`、`More posts`、`Relevant people`、`Who to follow` 等推荐区后停止继续抓取推荐内容。
- emoji 和文本会保留。
- 非文本内容会用类似 `[图片]` 的占位写入。

## Facebook 工具

Facebook 工具使用 Playwright 接管本地 Chrome。运行前需在浏览器中登录 Facebook。

### Facebook 博主作品采集

用途：输入博主主页链接，滚动采集主页下公开帖文。

输入：博主主页链接（每行一个）、是否限制时间、开始/结束日期、是否采集评论、是否强制获取精准发布时间。

可通过「参数配置」调整：最大采集帖子数、最大滚动次数、页面加载超时、滚动延迟、无新内容停止阈值、每次滚动像素、每批保存条数、评论最多采集数。

### Facebook 关键词搜索

用途：按关键词搜索 Facebook 公开帖文。

输入：关键词（每行一个）、是否限制时间、开始/结束日期、是否按最新排序、是否采集评论。

可通过「参数配置」调整：最大采集帖子数、最大滚动次数、页面加载超时、滚动延迟、无新内容停止阈值、每次滚动像素、每批保存条数。

## 数据处理工具

### AIGC 标题判断

用途：读取 XLSX 或 TXT 中的序号和标题，判断是否为 AIGC 内容，并识别主要语言。

两阶段判断：先用本地关键词和 Unicode 范围检测快速筛选，未确定的标题再发送到 DeepSeek 做最终判断。

输入：

- 待判断的 XLSX 文件路径
- 存放中间结果的调试 TXT 文件路径

可通过「参数配置」调整：LLM 温度、批次间等待秒数、是否信任本地判负结果。

运行前需要配置 `.env`。

### 关键词 XLSX 合并

用途：选择文件夹，合并文件名包含指定关键词的 `.xlsx` 文件，并重新生成连续序号、对齐表头。

输入：

- 文件夹路径
- 文件名关键词（默认 `keyword`）
- 合并平台过滤（可选）

## 输出文件

默认输出目录：

```txt
output/
```

按平台分目录保存，例如：

```txt
output/youtube/
output/tiktok/
output/x/
output/steam/
output/instagram/
output/facebook/
output/temp/
```

常见文件名示例：

```txt
x_profile_tweets_YYYYMMDD.xlsx
x_tweet_metrics_YYYYMMDD.xlsx
x_top_comments_YYYYMMDD.xlsx
steam_api_research_YYYYMMDD_HHMMSS.xlsx
steam_player_profiles_YYYYMMDD_HHMMSS.xlsx
tiktok_top_comments_YYYYMMDD.xlsx
```

## 运行建议

- 首次使用 TikTok、X/Twitter 或 Instagram 前，先运行任意对应平台工具，让程序打开浏览器，然后完成登录。
- Steam 工具走 API，不需要浏览器登录；如果要补采玩家画像或使用 IStoreService AppList，请提前准备 Steam Web API Key。
- 每个工具窗口提供「参数配置」按钮，可在启动任务前调整爬取行为参数（超时、等待间隔、停止阈值等）。修改后的参数会保留到下次打开。
- 任务运行中可点击「暂停」暂停采集并保留当前进度，点击「继续」从暂停点恢复；点击「停止」彻底终止任务。
- 不要频繁并发运行多个 X/Twitter 或 TikTok 工具，容易触发平台限制。
- 长列表任务建议分批输入，便于排查失败链接。
- 如果采集到 0 条，先手动打开对应链接确认页面是否公开可见、账号是否登录、评论区是否存在。
- 如果 X/Twitter 页面结构变化，评论层级、广告标记或推荐区边界可能需要同步调整选择器。

## 常见问题

### 缺少 Playwright

报错类似：

```txt
缺少依赖：playwright。请先安装 requirements.txt 中的依赖。
```

处理：

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### X/Twitter 采集不到内容

优先检查：

- 浏览器是否已经登录 X/Twitter。
- 链接是否公开可见。
- 页面是否显示登录弹窗、验证码、风控提示。
- 目标推文或主页是否已删除、受限或私密。

### TikTok 评论为空

优先检查：

- 视频页面是否真的有评论。
- 评论区是否被关闭。
- 是否需要登录才能看到评论。
- 页面是否出现地区、年龄、敏感内容或风控限制。

### YouTube 工具报 API 错误

优先检查：

- Google API Key 是否有效。
- YouTube Data API v3 是否启用。
- API 配额是否用完。
- 日期格式是否为 `YYYY-MM-DD`。

