# `merge-test` 收尾验收记录

## 固定环境

- 操作系统：Windows
- Python：3.13
- 默认 CI：无网络、无真实浏览器登录态
- 手工测试：仅使用 `test/manual/` 中的脚本

## 自动化门禁

| 检查 | 命令 | 结果 |
|---|---|---|
| Ruff | `python -m ruff check .` | 通过 |
| 默认单测 | `python -m pytest -q test` | 300 passed, 1 skipped, 2 subtests passed |
| manifest 导入 | `python -m pytest -q test/test_tool_imports.py` | 32 个 manifest 均可独立导入 |
| XLSX smoke | `python -m pytest -q test/test_xlsx_performance.py` | 通过 |
| XLSX benchmark | `python benchmarks/xlsx_write_benchmark.py` | 12 个场景通过，异常率 0 |

## 人工验收

- [ ] 主界面在 Python 3.13 环境正常启动。
- [ ] 所有 manifest 工具可打开并正常关闭。
- [ ] 连接用户自行启动的 Chrome/Edge 时不会终止其进程。
- [ ] 受管浏览器可以连接、取消、恢复并在应用退出时关闭。
- [ ] 远程 CDP 失败不会触发任何本地浏览器动作。
- [ ] 任务取消和异常退出时，已缓冲 XLSX 数据能够最终落盘。
- [ ] checkpoint 断点续跑不会复活已释放任务。

## CI 与基准证据

- CI 链接：待首次推送后填写。
- XLSX 基准环境和结果：见 `xlsx_性能基线.md` 与 `xlsx_性能基线.json`。
