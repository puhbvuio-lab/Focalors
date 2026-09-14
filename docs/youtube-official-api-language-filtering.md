# 基于 YouTube 官方 API 实现小语种区分

## 目标

这份文档只讨论一件事：

> 如果你要用 YouTube 官方 API 做关键词搜索，并把结果按小语种区分出来，应该怎么实现？

这里不依赖任何具体项目，也不假设你已经有现成工具。

---

## 1. 先说结论

如果只用 YouTube 官方 API，比较稳妥的实现方式不是“一步直接搜出纯净小语种结果”，而是两段式：

1. 搜索阶段尽量把结果偏向目标语言
2. 详情阶段再做严格语言过滤

也就是：

```text
关键词搜索 -> 拿到 video_id
         -> 拉视频详情
         -> 根据语言字段过滤
```

这是最实用、最稳定、也最容易落地的方案。

---

## 2. YouTube 官方 API 里真正有用的能力

实现小语种区分时，核心只需要两类接口。

### 2.1 `search.list`

用途：

- 根据关键词搜索视频
- 返回视频 ID
- 支持 `relevanceLanguage`
- 支持 `publishedAfter` / `publishedBefore`

你真正关心的搜索参数通常是：

- `q`
- `type=video`
- `part=id`
- `maxResults`
- `order`
- `pageToken`
- `relevanceLanguage`
- `publishedAfter`
- `publishedBefore`

### 2.2 `videos.list`

用途：

- 根据视频 ID 拉详情
- 读取语言元数据

你真正关心的字段通常是：

- `snippet.defaultAudioLanguage`
- `snippet.defaultLanguage`
- `snippet.title`
- `snippet.description`
- `snippet.publishedAt`

如果只是做语言过滤，前两个是核心字段。

---

## 3. 为什么不能只靠 `search.list`

很多人一开始会想：

> 我直接给 `search.list` 传 `relevanceLanguage=fr`，是不是就等于法语搜索了？

不是。

原因有两个：

### 3.1 `relevanceLanguage` 更像“排序偏向”，不是最终判定

它会让搜索结果更偏向某个语言，但不等于：

- 结果一定全是该语言
- 没命中的一定不是该语言

### 3.2 搜索结果本身没有足够可靠的语言判定信息

你最终还是要拉 `videos.list`，再看：

- `defaultAudioLanguage`
- `defaultLanguage`

所以工程上不要把 `search.list` 当成最终语言分类器。

---

## 4. 推荐的实现架构

## 4.1 总流程

```text
输入：
- keywords
- target_languages
- start_time / end_time（可选）

流程：
1. 规范化目标语种代码
2. 调 search.list 拿视频 ID
3. 调 videos.list 拉详情
4. 提取语言字段
5. 做本地语言匹配
6. 过滤不符合的结果
7. 输出保留结果
```

---

## 5. 第一步：规范化目标语种代码

你需要先把用户输入的语种代码变成统一格式。

推荐做法：

- 全部转小写
- 去掉首尾空格
- 支持逗号、分号、空格、换行分隔

例如：

```text
zh-CN, zh-TW; en
JA
```

转成：

```python
{"zh-cn", "zh-tw", "en", "ja"}
```

参考实现：

```python
import re

def parse_language_filter(value: str | None) -> set[str]:
    if not value:
        return set()
    return {
        part.strip().lower()
        for part in re.split(r"[,;\\s]+", str(value))
        if part.strip()
    }
```

---

## 6. 第二步：搜索阶段如何使用 `relevanceLanguage`

### 6.1 单语种场景

如果目标语种只有一个，例如：

```python
{"fr"}
```

推荐直接在 `search.list` 里加：

```text
relevanceLanguage=fr
```

这是最简单也最有效的做法。

### 6.2 多语种场景

如果目标语种有多个，例如：

```python
{"fr", "de", "es"}
```

有两种实现策略。

#### 策略 A：拆成多个单语种任务

分别跑：

- `relevanceLanguage=fr`
- `relevanceLanguage=de`
- `relevanceLanguage=es`

再合并结果。

优点：

- 搜索质量更稳
- 每个语言轨道更清晰

缺点：

- 请求次数更多

#### 策略 B：不传 `relevanceLanguage`

直接做通用搜索，然后在详情阶段统一过滤。

优点：

- 实现简单

缺点：

- 搜索阶段混入的异语内容会更多
- 后续过滤损耗更大

如果你的目标是做真正可比的小语种数据，通常更推荐策略 A。

---

## 7. 第三步：详情阶段拉回语言字段

搜索拿到的是视频 ID，不是最终结果。

接下来必须调用 `videos.list` 拉详情，并把以下字段请求回来：

```text
snippet.defaultAudioLanguage
snippet.defaultLanguage
```

推荐一起带上常用字段：

```text
snippet.title
snippet.description
snippet.publishedAt
```

示意：

```python
youtube.videos().list(
    part="snippet",
    id="id1,id2,id3",
    fields="items(id,snippet(title,description,publishedAt,defaultAudioLanguage,defaultLanguage))"
)
```

这里最重要的点是：

- 小语种判定依赖的是视频元数据
- 不是标题文本检测
- 不是简介文本检测

---

## 8. 第四步：本地语言判定规则

建议把语言判定拆成两步：

1. 提取候选语言
2. 判断是否匹配目标语种集合

### 8.1 语言提取优先级

推荐优先级：

1. `defaultAudioLanguage`
2. `defaultLanguage`

原因是：

- 音频语言通常更接近“视频实际主要语言”
- 文本默认语言更容易受标题/描述设置影响

示例实现：

```python
def detect_video_language(snippet: dict) -> tuple[str, str]:
    audio_language = (snippet.get("defaultAudioLanguage") or "").strip().lower()
    if audio_language:
        return audio_language, "defaultAudioLanguage"

    text_language = (snippet.get("defaultLanguage") or "").strip().lower()
    if text_language:
        return text_language, "defaultLanguage"

    return "", ""
```

### 8.2 匹配规则

推荐至少支持三种结果：

- `match`
- `missing`
- `mismatch`

推荐实现逻辑：

1. 没配置目标语种：直接通过
2. 视频没有语言字段：记为 `missing`
3. 完全匹配：通过
4. 前缀匹配：通过
5. 其他：`mismatch`

示例实现：

```python
def language_matches_snippet(snippet: dict, target_languages: set[str] | None) -> tuple[bool, str]:
    if not target_languages:
        return True, "disabled"

    language, source = detect_video_language(snippet)
    if not language:
        return False, "missing"

    if language in target_languages:
        return True, source

    lang_prefix = language.split("-")[0]
    if lang_prefix in target_languages:
        return True, f"{source}(prefix)"

    return False, "mismatch"
```

---

## 9. 为什么要支持前缀匹配

因为 YouTube 返回的语言经常是 BCP-47 风格：

- `fr-FR`
- `pt-BR`
- `zh-CN`
- `zh-TW`

而你的目标语种配置可能更粗：

- `fr`
- `pt`
- `zh`

如果不做前缀匹配，会漏掉很多本来应该保留的结果。

但这里也有一个边界：

- 如果你要严格区分 `zh-CN` 和 `zh-TW`
- 那目标配置就不能只写 `zh`

---

## 10. 时间过滤应该怎么做

如果你还需要限制时间窗，推荐直接在 `search.list` 阶段使用：

- `publishedAfter`
- `publishedBefore`

不要指望浏览器搜索页面替代这件事，因为：

- 页面搜索结果不稳定
- 过滤条件难以精确控制
- 结果重现性差

如果时间范围较大，推荐把时间切块搜索。

例如：

- 按 7 天一块
- 按 1 天一块
- 按 6 小时一块

原因是 `search.list` 在大时间范围下容易触发结果上限，切块可以提高覆盖率。

示意：

```text
2026-06-01 ~ 2026-06-07
2026-06-08 ~ 2026-06-14
2026-06-15 ~ 2026-06-21
...
```

---

## 11. 推荐的分页和过滤顺序

推荐顺序如下：

1. 用 `search.list` 分页拿视频 ID
2. 对视频 ID 去重
3. 每 50 个 ID 调一次 `videos.list`
4. 在本地做语言过滤
5. 再输出或进入下一步分析

原因：

- `search.list` 的结果不够干净
- `videos.list` 才有可靠的语言字段
- 先去重再拉详情可以减少请求量

---

## 12. 多语种任务的推荐策略

如果你是要做英语、日语、德语等多轨道对比，最佳实践通常不是“一次混跑”。

更推荐：

### 方案 A：按语言拆轨

例如同一关键词体系分别跑：

- 英文轨：`en`
- 日文轨：`ja`
- 德文轨：`de`

优点：

- 数据边界清晰
- 后续对比更容易解释

### 方案 B：多语种混跑再过滤

这种做法只有在你明确知道自己要什么时才建议使用。

因为它的问题是：

- 搜索阶段会引入更多噪声
- 每种语言的召回路径不一致
- 后续结果解释成本高

---

## 13. 这个方案解决不了什么

只用官方 API，这个方案解决的是：

- “按官方元数据区分语言”

它解决不了的是：

- 标题是法语但没标语言的视频
- 简介是德语但音频是英语的视频的复杂语义归类
- 多语言混说视频的主语言判断
- 同名异物、蹭热词、泛词噪声

所以一定要明确：

> 这不是内容语言识别，也不是语义相关性清洗。

如果你要进一步提纯结果，通常还要叠加：

- 标题/简介文本语言识别
- AI 语义相关性判断
- 人工复核

---

## 14. 最小可用伪代码

```python
def collect_youtube_videos_by_language(
    youtube,
    keyword: str,
    target_languages: set[str],
    max_results: int,
    start_time=None,
    end_time=None,
):
    relevance_language = ""
    if len(target_languages) == 1:
        relevance_language = next(iter(target_languages))

    video_ids = []
    next_page_token = None

    while len(video_ids) < max_results:
        params = {
            "part": "id",
            "q": keyword,
            "type": "video",
            "maxResults": min(50, max_results - len(video_ids)),
            "pageToken": next_page_token,
        }

        if relevance_language:
            params["relevanceLanguage"] = relevance_language
        if start_time:
            params["publishedAfter"] = start_time
        if end_time:
            params["publishedBefore"] = end_time

        resp = youtube.search().list(**params).execute()

        for item in resp.get("items", []):
            video_id = item.get("id", {}).get("videoId")
            if video_id and video_id not in video_ids:
                video_ids.append(video_id)

        next_page_token = resp.get("nextPageToken")
        if not next_page_token:
            break

    kept = []
    for batch in chunked(video_ids, 50):
        detail = youtube.videos().list(
            part="snippet",
            id=",".join(batch),
            fields="items(id,snippet(title,description,publishedAt,defaultAudioLanguage,defaultLanguage))",
        ).execute()

        for item in detail.get("items", []):
            snippet = item.get("snippet", {})
            ok, reason = language_matches_snippet(snippet, target_languages)
            if ok:
                kept.append(item)

    return kept
```

---

## 15. 一句话总结

如果你要用 YouTube 官方 API 实现小语种区分，最合理的做法是：

> 用 `search.list` 的 `relevanceLanguage` 做单语种搜索偏向，再用 `videos.list` 返回的 `defaultAudioLanguage` 和 `defaultLanguage` 做本地严格过滤。
