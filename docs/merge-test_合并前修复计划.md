# `merge-test` 合并前修复计划

> **目标分支**：`merge-test` → `main`
> **审查基线**：`003e820`（以本次 review 时的分支状态为准）
> **状态**：合并前整改
> **原则**：先修复用户环境安全、安装可用性与数据一致性；不在本轮引入新的平台化重构或大范围功能扩张。

---

## 1. 合并结论与门槛

当前结论为 **Request Changes**。在以下两项未完成前，禁止将 `merge-test` 合并到 `main`：

1. **浏览器/CDP 恢复逻辑不得结束用户的全部 Chrome/Edge 进程。**
2. **标准安装后，所有 manifest 暴露的工具必须可被安全导入，或能明确显示缺失依赖。**

完成硬门槛后，再处理共享模块清理、checkpoint 并发一致性、XLSX 写入性能和 CI 覆盖等问题。

---

## 2. 范围与非目标

### 2.1 本轮范围

- `src/core/browser.py` 的 CDP 探测、拉起、恢复与进程生命周期。
- `requirements.txt`、工具依赖声明与启动前自检。
- 一次性修复脚本、旧文件副本与共享函数归属。
- `src/core/task_checkpoint.py` 的并发状态一致性。
- `src/core/xlsx.py` 的大数据写入策略与性能基线。
- CI Python 版本矩阵、manifest 工具入口 smoke test。
- 被删除设计文档的迁移确认。

### 2.2 明确非目标

本轮不做：

- Web 控制台、Worker Agent、队列或数据库平台化。
- 全量 Runner SDK 重写。
- 采集平台选择器和业务规则的大规模重构。
- Kubernetes、Kafka、NATS、对象存储等基础设施引入。
- 不具备复现依据的“性能优化”。

---

## 3. 提交批次总览

| 批次 | 名称 | 优先级 | 是否阻断合并 |
|---|---|---:|---:|
| M1 | CDP 生命周期安全修复 | P0 | 是 |
| M2 | 依赖声明与工具可用性检查 | P0 | 是 |
| M3 | 临时文件清理与共享模块归位 | P1 | 强烈建议 |
| M4 | TaskCheckpoint 并发一致性修复 | P1 | 建议 |
| M5 | XLSX 写入性能基线与落盘策略 | P1 | 建议 |
| M6 | CI 与文档收口 | P1 | 建议 |

建议每个批次独立提交、独立测试，避免将行为变更、清理和性能改造混在一个大提交中。

---

# M1：CDP 生命周期安全修复

## 目标

将浏览器恢复行为从“按进程名清理”改为“只管理本程序启动且已记录 PID 的浏览器实例”，确保采集器故障不会影响用户手动打开的 Chrome / Edge。

## 当前风险

- 恢复逻辑可能使用 `taskkill /F /IM chrome.exe` 或 `msedge.exe`。
- 这种方式会结束机器中所有同名浏览器进程。
- 远程 CDP 地址不应触发本机浏览器清理或拉起。
- 仅依据 HTTP 200 判定 CDP 可用，无法区分普通 HTTP 服务与真实 CDP 服务。
- “端口被占用”与“CDP 可用”属于不同状态，不能混用。

## 改造要求

### 1. 新增浏览器进程注册信息

建议新增内部数据结构：

```python
@dataclass
class ManagedBrowserProcess:
    pid: int
    browser_name: str
    host: str
    port: int
    user_data_dir: str | None
    launched_by_app: bool
    started_at: datetime
```

要求：

- 仅当当前应用主动启动浏览器时写入记录。
- 重启或退出时只对 `launched_by_app=True` 且 PID 仍存在的进程执行终止。
- 若浏览器并非本程序启动，则只能连接或提示，不得终止。

### 2. 区分本地与远程 CDP

```python
is_local = host in {"127.0.0.1", "localhost", "::1"}
```

规则：

- 远程地址：仅尝试连接；失败时返回明确错误，不执行本地启动、端口清理或进程终止。
- 本地地址：仅可管理记录在 `ManagedBrowserProcess` 中的 PID。
- 非本机地址不允许因“恢复失败”误伤本地浏览器。

### 3. 重写 CDP 健康检查

拆成两个函数：

```python
def is_tcp_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    ...

def is_valid_cdp_endpoint(base_url: str, timeout: float = 2.0) -> bool:
    ...
```

验收逻辑：

1. TCP 可连接不等于 CDP 可用。
2. 调用 `/json/version`。
3. 必须成功解析 JSON。
4. JSON 至少包含 `webSocketDebuggerUrl`，并可选校验 `Browser` 字段。
5. 普通 HTTP 200、HTTP 400、空 JSON、非 JSON 都应被识别为“非有效 CDP”。

### 4. 删除全局杀进程路径

禁止保留：

```text
taskkill /F /IM chrome.exe
taskkill /F /IM msedge.exe
```

允许的终止方式：

- 按已记录 PID 终止。
- Windows 下可使用 `taskkill /PID <pid> /T /F`，但必须先确认该 PID 属于当前应用管理的浏览器。
- 若 PID 不存在或无法终止，只记录诊断信息，不进行进程名级别兜底。

## 测试

新增 `test/test_browser_lifecycle.py`，至少覆盖：

- 用户手动启动 Chrome 时，恢复流程不调用终止操作。
- 管理进程可按 PID 被终止。
- 远程 CDP 地址失败时，不尝试启动或清理本地浏览器。
- 普通 HTTP 200 服务不是 CDP。
- HTTP 400 服务不是 CDP。
- `/json/version` 返回合法 CDP JSON 时通过。
- 端口占用但非 CDP 时给出明确错误。
- Chrome 启动失败、PID 丢失、终止失败时不会升级为全局 kill。

## 验收标准

- 仓库中不再出现按 `chrome.exe` / `msedge.exe` 全局杀进程的生产代码路径。
- 任何恢复流程只能终止受管 PID。
- 远程 CDP 地址不会触发本机浏览器管理动作。
- 所有测试通过，且新增测试在 CI 中执行。

---

# M2：依赖声明与工具可用性检查

## 目标

确保用户通过标准安装流程后，不会出现“主程序能启动、点击某个 manifest 工具才因 ImportError 崩溃”的情况。

## 当前风险

异常检测工具顶层依赖 `pandas`、`numpy`，但标准依赖清单未完整表达该要求。此类问题会导致安装成功的假象，并将失败延后到用户点击工具时。

## 改造方案

### 方案 A：将依赖列入标准 requirements（默认推荐）

在 `requirements.txt` 中加入：

```text
numpy>=<确定的最低版本>
pandas>=<确定的最低版本>
```

版本选择要求：

- 与项目声明支持的 Python 版本兼容。
- 在 Windows CI 中完成实际安装和测试。
- 不要只根据本机环境写死版本。

### 方案 B：将工具作为可选依赖

仅当异常检测确实不应成为默认安装功能时采用：

```text
requirements.txt
requirements-anomaly.txt
```

或使用 extras：

```text
pip install -e ".[anomaly]"
```

同时工具 manifest / 启动器必须能显示：

```text
缺少可选依赖：pandas, numpy
安装方式：pip install -r requirements-anomaly.txt
```

不得让 UI 在导入阶段直接崩溃。

## 工具导入 smoke test

新增 `test/test_tool_imports.py`：

1. 扫描所有 `*.manifest.json`。
2. 读取 `entrypoint` 或实际窗口入口。
3. 尝试导入对应模块。
4. 对可选依赖工具，断言抛出的是可识别的“依赖缺失”状态，而不是无上下文 `ImportError`。
5. 不要求真实启动 GUI 或访问网络。

建议辅助函数：

```python
def assert_tool_importable_or_explicitly_unavailable(tool_spec) -> None:
    ...
```

## 验收标准

- `pip install -r requirements.txt` 后，标准工具均可导入。
- 若使用可选依赖，工具列表和启动界面均能显示缺失依赖原因。
- 新 smoke test 纳入 CI。
- 文档写清标准安装和可选安装路径。

---

# M3：临时修复文件清理与共享模块归位

## 目标

移除一次性救火脚本、旧文件副本和通过文本/AST 追加生产函数的临时方案，将实际复用逻辑整理到稳定模块，避免未来维护者误用或重复执行。

## 待处理内容

清理以下类型文件：

- `*_old`、备份副本、二进制旧源码。
- 仅服务于一次提交恢复的 `extract_missing_funcs*.py`。
- 通过正则改写测试或生产文件的 `remove_mock.py`。
- 只依赖 `git show HEAD~1` 等历史上下文才能工作的迁移脚本。

## 模块归位建议

将共享 XLSX / schema / JSON 解析逻辑迁移到明确位置，例如：

```text
src/
  core/
    xlsx.py                 # 通用写入、原子保存、输出路径
  processing/
    xlsx_schema.py          # 表头、字段映射、schema 差异
    result_parsing.py       # JSON / 结果行解析
```

规则：

- 平台工具只保留自身业务解析与 UI 适配。
- 不把跨工具函数长期堆在 `keyword_candidate_validator.py` 之类业务模块末尾。
- 共享函数必须具备独立单元测试。
- `windows.py` 等 UI 模块只能导入正式公共模块，不得依赖临时恢复文件。

## 安全迁移步骤

1. 为当前被复用函数补单元测试，先锁定行为。
2. 迁移到公共模块，保留临时兼容导入一轮提交。
3. 更新调用方。
4. 删除旧定义和临时脚本。
5. 用 `ruff`、`pytest` 和 tool import smoke test 验证。
6. 通过 `git grep` 确认没有旧路径引用。

## 验收标准

- 仓库不保留临时提取、正则改写、旧源码副本。
- 每个共享函数具有明确归属与单测。
- 业务模块不再承担无关共享工具职责。
- 所有工具窗口能够正常导入。

---

# M4：TaskCheckpoint 并发一致性修复

## 目标

防止两个执行实例交错读写 checkpoint 时，旧快照把已经释放的 `active_runs` 或 `active` 状态重新写回文件。

## 问题模型

典型竞态：

```text
实例 A：读取 checkpoint，缓存旧 active 状态
实例 B：完成任务并 release_item / close_run，持久化新状态
实例 A：随后调用 save，使用旧缓存回写 active 状态
结果：已释放项目重新显示为 active
```

文件锁只能保证一次写操作不重叠，不能防止过期内存快照覆盖新状态。

## 改造原则

### 1. 运行态字段禁止通用 `save()` 回写

以下字段只能通过专用锁内操作修改：

```text
active_runs
active
claims
lease_expiry
```

`save()` 仅允许写入：

```text
completed
failed
metadata
metrics
artifacts
```

或将状态拆为独立的、带版本号的操作日志。

### 2. 增加版本号或乐观锁

建议 checkpoint 中维护：

```json
{
  "revision": 42
}
```

写入流程：

1. 锁内读取最新 revision。
2. 基于最新数据执行一次明确的状态变更。
3. `revision += 1`。
4. 原子写入。

不要使用“读取旧对象后整块 update”覆盖运行态。

### 3. 专用接口

```python
def claim_item(item_id: str, run_id: str, ttl_seconds: int) -> ClaimResult:
    ...

def release_item(item_id: str, run_id: str) -> None:
    ...

def register_run(run_id: str) -> None:
    ...

def close_run(run_id: str) -> None:
    ...
```

所有接口必须在同一临界区内读取、判断、更新、落盘。

## 测试

新增竞态回归测试：

- 实例 A 读取旧快照。
- 实例 B `release_item()` / `close_run()` 并写入。
- 实例 A 再 `save()`。
- 断言实例 B 的释放结果不会被复活。

另覆盖：

- lease 过期回收。
- 同一 item 多实例 claim。
- 崩溃后孤儿 active 状态恢复。
- Windows 文件锁异常时不会写出损坏 JSON。

## 验收标准

- 没有任何通用保存路径可覆盖 `active_runs` / `active`。
- 并发竞态测试稳定通过。
- 运行态变更具备可解释的版本或操作语义。

---

# M5：XLSX 写入性能基线与落盘策略

## 目标

确认当前 XLSX 写入在大任务下的真实成本，并避免“每写 500 行就全量保存工作簿”造成重复序列化、CPU 升高、内存膨胀与磁盘抖动。

## 现状判断

普通 `openpyxl.Workbook()` 的重复 `save()` 并不等于流式写入。随着累计行数增长，每次保存成本会持续上升。将临时文件原子替换只能提升文件一致性，不能解决重复序列化成本。

## 实施分两步

### M5.1：先建立性能基线

新增基准脚本或 pytest benchmark，固定记录：

- 10,000 行。
- 50,000 行。
- 100,000 行（硬件允许时）。
- 单 sheet / 多 sheet。
- 每 500 行保存、每 5,000 行保存、仅结束时保存。

记录指标：

```text
总耗时
追加耗时
保存次数
最大 RSS 内存
输出文件大小
每 1,000 行平均耗时
异常率
```

不得只记录“感觉更快”。

### M5.2：确定正式策略

推荐默认策略：

1. **实时结果与最终 XLSX 解耦**
   运行期写 JSONL 或 CSV 分段文件；任务结束时一次性生成 XLSX。

2. **checkpoint 与 XLSX 解耦**
   checkpoint 存状态与断点；不将 XLSX 作为高频 checkpoint。

3. **保留用户可见中间成果时**
   降低 XLSX 保存频率，默认仅在完成、明确保存、或较大批次阈值时保存。

4. **write-only 模式的适用边界**
   仅在不需要回读/修改同一工作簿时使用 `write_only=True`。
   使用前必须确认现有多 sheet、样式、恢复机制不依赖普通工作簿随机访问。

## 验收标准

- 有可重复的性能基线数据，而不是主观判断。
- 默认策略不再每 500 行完整重写工作簿。
- 大任务的 checkpoint 不依赖频繁 XLSX 保存。
- 10k / 50k 行回归测试或基准结果被记录在开发文档中。

---

# M6：CI、文档与合并收口

## 目标

让 CI 覆盖本轮修复，并确认删除的设计文档不是误删。

## CI 改造

### 1. Python 版本

项目仅支持 Python 3.13，CI 与安装器必须使用同一版本：

```yaml
python-version: ["3.13"]
```

Windows 继续作为主验证环境；若条件允许，增加至少一个非 Windows import/lint 任务用于发现路径依赖。

### 2. CI 必跑步骤

```bash
python -m pytest -q test
python -m ruff check .
```

并确保包含：

- `test_browser_lifecycle.py`
- `test_tool_imports.py`
- `test_task_checkpoint.py`
- XLSX 性能 smoke / 基准的轻量版本

网络、真实登录、真实平台 API 测试必须与纯单测分层，避免 CI 不稳定：

```text
test/unit/
test/integration/
test/manual/
```

默认 CI 只跑不依赖网络和浏览器登录态的测试。

## 文档处理

最后一个提交删除的 P0 执行核心设计、YouTube API 语言过滤等文档需逐项确认：

| 文档类型 | 处理原则 |
|---|---|
| 仍指导当前实现 | 恢复或迁移到 `docs/` |
| 已被新的设计文档替代 | 在新文档中建立替代说明 |
| 已失效 | 在 PR 描述中说明删除原因 |

禁止无说明删除仍影响开发约定的设计文档。

## 合并前检查清单

```text
[x] M1：没有全局 Chrome/Edge 杀进程路径
[x] M1：远程 CDP 不触发本地恢复
[x] M2：标准安装依赖流程可用
[x] M2：所有 manifest 工具可导入
[x] M3：临时脚本、旧副本已清理
[x] M4：checkpoint 陈旧快照回写竞态已修复
[x] M5：已有 XLSX 性能基线与正式落盘策略
[ ] M6：Python 3.13 CI 通过
[x] M6：pytest、ruff、工具导入 smoke test 均通过
[x] M6：删除文档已恢复、迁移或有明确弃用说明
[ ] M6：人工验证采集器启动、取消、恢复和关闭路径
```

---

## 4. 推荐提交顺序

```text
C1  fix(browser): 管理 CDP 浏览器 PID，移除全局 taskkill
C2  test(browser): 覆盖本地/远程 CDP 与非 CDP 端口场景
C3  fix(deps): 补齐异常检测依赖或新增可选依赖自检
C4  test(tools): 增加 manifest 工具入口 import smoke test
C5  refactor(processing): 提取共享解析/XLSX schema 模块，删除临时恢复文件
C6  fix(checkpoint): 防止旧快照回写 active 状态
C7  test(checkpoint): 增加跨实例陈旧保存竞态回归
C8  perf(xlsx): 建立基准并调整 checkpoint/XLSX 落盘策略
C9  ci: 统一 Python 3.13 与完整测试门禁
C10 docs: 恢复、迁移或明确弃用删除的设计文档
```

建议 C1～C4 完成并通过 CI 后，先进行一次安全性复审；这时才允许继续处理 C5～C10。

---

## 5. 最终验收

`merge-test` 可以进入合并复审的最低条件：

1. 用户的 Chrome / Edge 不会被采集器的恢复机制误杀。
2. 所有暴露工具在标准安装下可用，或有明确的依赖缺失提示。
3. 临时脚本和旧文件副本不进入主分支。
4. checkpoint 的运行态不会被陈旧对象复活。
5. XLSX 大任务的性能策略有实测基线支持。
6. CI 覆盖 Python 3.13、完整单测、lint、工具导入。
7. 删除的设计文档有明确去向。
8. 合并前人工验证：浏览器连接、取消、恢复、产物写入、工具启动与主界面启动。
