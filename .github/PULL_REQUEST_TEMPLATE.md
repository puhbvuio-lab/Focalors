## 📝 变更说明

<!-- 简要描述这个 PR 做了什么，为什么要做 -->

## 🔗 关联 Issue 与需求拆解

<!-- 1. 使用关键词链接关联的 issue，例如：Closes #12、Fixes #34 -->
<!-- 2. 基于对 Issue 的需求拆解，清晰列出本次 PR 处理的范围： -->

- [x] 已修复/实现：<!-- 简述本 PR 解决的具体拆解项 -->
- [ ] 未修复/计划后续：<!-- 简述该 Issue 下仍未解决的部分（若已全部解决可删去此项） -->

## 🧪 测试

<!-- 描述你做了哪些测试来验证这个改动 -->

- [ ] `ruff check .` 通过
- [ ] `python test/test_visibility.py` 通过
- [ ] 手动测试：
  - 平台：<!-- YouTube / TikTok / X/Twitter / Instagram / 数据处理 -->
  - 工具：<!-- 如 youtube_keyword_mining -->
  - 验证结果：<!-- 如「关键词搜索返回正确结果，输出 xlsx 字段完整」 -->

## 📋 检查清单

- [ ] 代码风格与项目现有代码一致（注释密度、命名风格、缩进等）
- [ ] 核心逻辑有中文注释说明
- [ ] 采集循环中正确使用了 `should_stop(stop_event)` 和 `wait_if_paused(pause_event, stop_event)` 做协作式停止/暂停检查
- [ ] 使用 `interruptible_sleep` / `random_cooldown` 代替 `time.sleep`
- [ ] 输出路径使用 `build_output_path(platform, filename)` 构建
- [ ] 新增依赖已加入 `requirements.txt`（如果有）
- [ ] 新增工具已创建对应的 `manifest.json` 文件（如果添加了新工具）
- [ ] 没有将 API Key、密码等敏感信息硬编码在代码中

## 📸 截图（可选）

<!-- 如果是 UI 相关改动，附上前后对比截图 -->

## ⚠️ 注意事项

<!-- 如果有破坏性变更、需要额外迁移步骤、或其他 reviewer 需要注意的事项，请在此说明 -->
