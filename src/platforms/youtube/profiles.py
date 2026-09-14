# -*- coding: utf-8 -*-
"""YouTube 博主详情数据采集与解析模块。

本模块提供基于 Google YouTube v3 API 的博主/频道主页信息采集，
支持多种格式的 YouTube 频道 URL 识别、归一化与元数据（如名称、ID、粉丝数、简介）抓取。
"""

from __future__ import annotations

import os
import json
import time
from pathlib import Path
from urllib.parse import urlparse
from googleapiclient.errors import HttpError

from src.core import (
    ArtifactRef,
    RunError,
    RunOutcome,
    RunStatus,
    XlsxRowWriter,
    build_platform_run_output_dir,
    feedback_from_config,
    generate_run_id,
    log_error,
    log_line,
    sanitize_csv_row,
    should_stop,
    wait_if_paused,
)
from src.platforms.youtube.keyword import (
    YouTubeClientPool,
    execute_with_retry,
    is_youtube_api_key_suspended_error,
)

# Excel 输出表头与诊断字段定义
CSV_FIELDS = [
    "输入链接", "作者主页链接", "作者名称", "作者ID", "粉丝量", "作者简介",
    "input_url", "normalized_url", "resolution_method", "resolved_channel_id", "resolution_status", "error_code", "error_message"
]


def validate_and_normalize_youtube_url(url: str) -> tuple[str, str, str]:
    """严格验证并归一化 YouTube 频道主页 URL。

    仅接受标准域名且路径为 /channel/<id>, /@<handle>, 或 /user/<name> 的主页链接。
    拒绝伪造域名、纯域名、视频、播放列表及不可直接验证的自定义别名 /c/ 路径。

    Returns:
        tuple[str, str, str]: (标准化URL, 解析方式, 识别特征值)。
    """
    raw_url = (url or "").strip()
    if not raw_url:
        raise ValueError("invalid_url: URL为空")

    if raw_url.startswith("//"):
        url_to_parse = "https:" + raw_url
    elif not raw_url.startswith("http"):
        url_to_parse = "https://" + raw_url
    else:
        url_to_parse = raw_url

    try:
        parsed = urlparse(url_to_parse)
    except Exception as exc:
        raise ValueError(f"invalid_url: URL解析异常: {exc}")

    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"invalid_url: 不支持的 scheme: {parsed.scheme}")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("invalid_url: 缺少域名标识")

    # Domain checking
    allowed_hosts = {"youtube.com", "www.youtube.com", "m.youtube.com"}
    if hostname not in allowed_hosts:
        raise ValueError(f"invalid_url: 不支持的域名 {hostname}")

    path = parsed.path or ""
    parts = [p for p in path.split("/") if p]
    if not parts:
        raise ValueError("invalid_url: 仅包含域名，无有效路径")

    # 2. 过滤不支持的视频、播放列表或短视频等资源链接
    if parts[0] in {"watch", "playlist", "shorts", "embed", "v", "shared"}:
        raise ValueError(f"invalid_url: 不支持的资源链接类型 {parts[0]}")

    # 3. /channel/<channel_id> 路径校验
    if parts[0] == "channel":
        if len(parts) != 2:
            raise ValueError("invalid_url: channel 路径格式错误或包含额外段")
        channel_id = parts[1].strip()
        if not channel_id.startswith("UC"):
            raise ValueError("invalid_url: channel_id 格式不符合 UC... 规范")
        return f"https://www.youtube.com/channel/{channel_id}", "channel_id", channel_id

    # 4. /@<handle> 路径校验
    if parts[0].startswith("@"):
        if len(parts) != 1:
            raise ValueError("invalid_url: handle 路径包含额外段")
        handle = parts[0].strip()
        if len(handle) < 2:
            raise ValueError("invalid_url: handle 标识不完整")
        return f"https://www.youtube.com/{handle}", "handle", handle

    # 5. /user/<username> 路径校验
    if parts[0] == "user":
        if len(parts) != 2:
            raise ValueError("invalid_url: user 路径格式错误或包含额外段")
        username = parts[1].strip()
        if not username:
            raise ValueError("invalid_url: 缺失 username")
        return f"https://www.youtube.com/user/{username}", "username", username

    # 6. 拒绝 /c/ 或 custom 别名等不可验证或需二次搜索路径
    if parts[0] in {"c", "custom"}:
        raise ValueError(f"invalid_url: 不支持自定义别名 /c/ 或 /custom/ 路径: {parts[0]}")

    raise ValueError("invalid_url: 未知的 YouTube 路径格式，无法直接解析")


def classify_api_error(exc: Exception) -> str:
    """将 API 请求异常归类为具体的标准错误标识代码。"""
    if isinstance(exc, HttpError):
        status = exc.resp.status
        content = exc.content.decode("utf-8", errors="ignore") if exc.content else ""

        if is_youtube_api_key_suspended_error(exc):
            return "api_key_suspended"
        
        # Try to parse Google API response JSON errors
        reason = ""
        try:
            data = json.loads(content)
            errors = data.get("error", {}).get("errors", [])
            if errors:
                reason = errors[0].get("reason", "")
        except Exception:
            pass

        if status == 400:
            return "invalid_request"
        if status == 401:
            return "auth_invalid"
        if status == 403:
            reason_lower = reason.lower()
            content_lower = content.lower()
            if "quota" in reason_lower or "limit" in reason_lower or "quota" in content_lower or "limit" in content_lower:
                return "quota_exhausted"
            return "forbidden_resource"
        if status == 404:
            return "not_found"
        if status == 429:
            return "rate_limited"
        if status >= 500:
            return "transient_network"
        return "unknown"

    exc_str = str(exc).lower()
    if "timeout" in exc_str or "connection" in exc_str or "http" in exc_str:
        return "transient_network"
    return "unknown"


def resolve_channel(
    client_pool: YouTubeClientPool,
    normalized_url: str,
    hint_type: str,
    hint_value: str,
    log_callback=None,
) -> dict:
    """调用 YouTube API 解析并获取频道的原始元数据。

    Args:
        client_pool: YouTubeClientPool 实例。
        normalized_url: 标准化的主页。
        hint_type: 识别类型 (channel_id / handle / username)。
        hint_value: 对应的特征键值。
        log_callback: 日志记录回调函数。

    Returns:
        dict: API 返回的频道元数据字典，若未找到则返回空字典。
    """
    def _execute_req(build_req):
        while True:
            request_client_idx = client_pool.current_idx
            try:
                return execute_with_retry(build_req(), None)
            except HttpError as e:
                err_code = classify_api_error(e)
                # 资源权限不足不能换 Key；Key 自身被暂停则必须换 Key。
                if err_code in ["quota_exhausted", "auth_invalid", "api_key_suspended"]:
                    old_idx = client_pool.current_idx
                    if client_pool.next_client(request_client_idx):
                        new_idx = client_pool.current_idx
                        if log_callback:
                            log_line(log_callback, f"API Key 轮换：原因码={err_code}，旧 Key 索引={old_idx}，新 Key 索引={new_idx}")
                        continue
                raise e

    if hint_type == "channel_id":
        response = _execute_req(lambda: client_pool.client.channels().list(part="snippet,statistics", id=hint_value))
    elif hint_type == "username":
        response = _execute_req(lambda: client_pool.client.channels().list(part="snippet,statistics", forUsername=hint_value))
    elif hint_type == "handle":
        response = _execute_req(lambda: client_pool.client.channels().list(part="snippet,statistics", forHandle=hint_value))
    else:
        return {}

    items = response.get("items", [])
    return items[0] if items else {}


def channel_row(profile_url: str, item: dict) -> dict:
    """提取频道元数据字典为符合保存格式的规范字典。"""
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    channel_id = item.get("id", "")
    description = (snippet.get("description") or "").replace("\n", " | ").replace("\r", "").strip()
    return {
        "作者主页链接": profile_url,
        "作者名称": snippet.get("title", ""),
        "作者ID": channel_id,
        "粉丝量": stats.get("subscriberCount", "已隐藏"),
        "作者简介": description,
    }


def run_channel_spider(
    api_keys: list[str],
    txt_file_path,
    log_callback,
    finish_callback,
    stop_event=None,
    config=None,
    pause_event=None,
    run_id: str | None = None,
) -> RunOutcome:
    """运行 YouTube 频道/博主元数据获取任务的驱动入口函数。

    每次执行在 output/youtube/profiles/<run_id>/ 目录下生成独立产物与报告。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    config = dict(config or {})
    feedback = feedback_from_config(config)

    # 1. 独占式唯一运行目录创建（带碰撞重试与冲突拦截）
    run_dir = None
    if run_id:
        try:
            run_dir = build_platform_run_output_dir("youtube", "profiles", run_id)
            actual_run_id = run_id
        except FileExistsError:
            outcome = RunOutcome(run_id=run_id, tool_id="youtube_profiles", status=RunStatus.FAILED)
            outcome.errors.append(RunError(code="RUN_ID_CONFLICT", message=f"任务ID冲突，输出目录已存在: {run_id}"))
            finish_callback(outcome)
            return outcome
    else:
        for attempt in range(3):
            actual_run_id = generate_run_id()
            try:
                run_dir = build_platform_run_output_dir("youtube", "profiles", actual_run_id)
                break
            except FileExistsError:
                if attempt == 2:
                    outcome = RunOutcome(run_id="unknown", tool_id="youtube_profiles", status=RunStatus.FAILED)
                    outcome.errors.append(RunError(code="RUN_ID_CONFLICT", message="自动生成唯一任务目录尝试失败"))
                    finish_callback(outcome)
                    return outcome

    final_xlsx_path = run_dir / "youtube_profiles.xlsx"
    final_report_path = run_dir / "youtube_profiles_report.json"

    outcome = RunOutcome(
        run_id=actual_run_id,
        tool_id="youtube_profiles",
        status=RunStatus.SUCCEEDED,
    )

    try:
        if not os.path.exists(txt_file_path):
            raise FileNotFoundError(f"输入文件 {txt_file_path} 不存在")

        with open(txt_file_path, "r", encoding="utf-8-sig") as f:
            profile_urls = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]

        outcome.stats.input_count = len(profile_urls)

        if not profile_urls:
            outcome.status = RunStatus.FAILED
            outcome.errors.append(RunError(code="invalid_url", message="TXT 输入文件中没有发现任何博主链接。"))
            outcome.save_to_json(final_report_path)
            finish_callback(outcome)
            return outcome

        if feedback is not None:
            feedback.set_total(len(profile_urls) + 1, phase="博主信息采集")
            feedback.set_activity(
                "正在准备博主任务",
                stop_reason="正在初始化 YouTube API 客户端。",
            )

        # 实例化 YouTube V3 API 服务客户端池
        client_pool = YouTubeClientPool(api_keys)

        max_parallel_tabs = int(config.get("max_parallel_tabs", 3))
        writer = XlsxRowWriter(str(final_xlsx_path), CSV_FIELDS)

        writer_lock = threading.Lock()
        log_lock = threading.Lock()
        stats_lock = threading.Lock()

        def make_thread_log(base_log_callback, lock, prefix):
            def wrapped(msg):
                if base_log_callback:
                    with lock:
                        base_log_callback(f"[{prefix}] {msg}")
            return wrapped

        def worker(index, input_url):
            if should_stop(stop_event):
                return
            wait_if_paused(pause_event, stop_event)

            thread_log = make_thread_log(log_callback, log_lock, f"通道 {index}")
            thread_log(f"开始解析作者：{input_url}")
            if feedback is not None:
                feedback.set_activity(
                    f"正在解析博主 {index}/{len(profile_urls)}",
                    stop_reason="仍有已发送的 YouTube API 请求正在等待响应。",
                )

            norm_url = ""
            res_method = ""
            res_val = ""
            resolved_id = ""
            res_status = "failed"
            err_code = ""
            err_msg = ""

            try:
                # 1. 本地合法性校验
                norm_url, res_method, res_val = validate_and_normalize_youtube_url(input_url)

                # 2. 获取 API 结果
                item = resolve_channel(client_pool, norm_url, res_method, res_val, log_callback=thread_log)
                if not item:
                    raise ValueError("not_found: 频道在 YouTube 官方平台不存在或已被删除")

                row = channel_row(norm_url, item)
                resolved_id = row["作者ID"]
                res_status = "success"

                with writer_lock:
                    if feedback is not None:
                        feedback.set_activity(
                            f"正在写入博主 {index} 的结果",
                            stop_reason="正在等待 XLSX 数据写入完成。",
                        )
                    writer.writerow(
                        sanitize_csv_row({
                            "输入链接": input_url,
                            "作者主页链接": norm_url,
                            "作者名称": row["作者名称"],
                            "作者ID": resolved_id,
                            "粉丝量": row["粉丝量"],
                            "作者简介": row["作者简介"],
                            "input_url": input_url,
                            "normalized_url": norm_url,
                            "resolution_method": res_method,
                            "resolved_channel_id": resolved_id,
                            "resolution_status": res_status,
                            "error_code": "",
                            "error_message": "",
                        })
                    )
                thread_log(f"成功：{row['作者名称']} | 粉丝量：{row['粉丝量']}")
                with stats_lock:
                    outcome.stats.success_count += 1

            except ValueError as ve:
                err_msg = str(ve)
                if ":" in err_msg:
                    parts = err_msg.split(":", 1)
                    err_code = parts[0].strip()
                    err_msg = parts[1].strip()
                else:
                    err_code = "invalid_url"

                thread_log(f"[WARN] 解析失败 ({err_code})：{err_msg}")
                with writer_lock:
                    if feedback is not None:
                        feedback.set_activity(
                            f"正在写入博主 {index} 的失败状态",
                            stop_reason="正在等待 XLSX 数据写入完成。",
                        )
                    writer.writerow(
                        sanitize_csv_row({
                            "输入链接": input_url,
                            "作者主页链接": "",
                            "作者名称": "未找到",
                            "作者ID": "",
                            "粉丝量": "",
                            "作者简介": "",
                            "input_url": input_url,
                            "normalized_url": "",
                            "resolution_method": "",
                            "resolved_channel_id": "",
                            "resolution_status": "failed",
                            "error_code": err_code,
                            "error_message": err_msg,
                        })
                    )
                with stats_lock:
                    outcome.stats.failed_count += 1
                    outcome.errors.append(RunError(code=err_code, message=err_msg, item=input_url))

            except Exception as exc:
                err_code = classify_api_error(exc)
                err_msg = str(exc)
                thread_log(f"[WARN] 请求异常 ({err_code})：{err_msg}")
                with writer_lock:
                    if feedback is not None:
                        feedback.set_activity(
                            f"正在写入博主 {index} 的失败状态",
                            stop_reason="正在等待 XLSX 数据写入完成。",
                        )
                    writer.writerow(
                        sanitize_csv_row({
                            "输入链接": input_url,
                            "作者主页链接": "",
                            "作者名称": "未找到",
                            "作者ID": "",
                            "粉丝量": "",
                            "作者简介": "",
                            "input_url": input_url,
                            "normalized_url": "",
                            "resolution_method": "",
                            "resolved_channel_id": "",
                            "resolution_status": "failed",
                            "error_code": err_code,
                            "error_message": err_msg,
                        })
                    )
                with stats_lock:
                    outcome.stats.failed_count += 1
                    outcome.errors.append(RunError(code=err_code, message=err_msg, item=input_url))

        # 并发跑任务 (有界提交)
        cancelled = False
        with ThreadPoolExecutor(max_workers=max_parallel_tabs) as executor:
            futures = {}
            url_iterator = enumerate(profile_urls, 1)

            # 初始提交最大并发数的任务数
            for _ in range(max_parallel_tabs):
                try:
                    idx, url = next(url_iterator)
                    f = executor.submit(worker, idx, url)
                    futures[f] = url
                except StopIteration:
                    break

            while futures:
                # 检查已完成的任务
                done = []
                for f in futures:
                    if f.done():
                        done.append(f)
                if not done:
                    time.sleep(0.05)
                    continue

                for f in done:
                    url = futures.pop(f)
                    try:
                        f.result()
                    except Exception as exc:
                        log_error(log_callback, f"线程执行抛错: {exc}")
                    if feedback is not None:
                        feedback.advance(1, activity="已完成一个博主处理单元", stop_reason="")

                # 若未停止，继续提交新任务
                if not should_stop(stop_event):
                    try:
                        idx, url = next(url_iterator)
                        f = executor.submit(worker, idx, url)
                        futures[f] = url
                    except StopIteration:
                        pass
                else:
                    cancelled = True
                    for f in futures:
                        f.cancel()
                    break

        # 中断状态判别 (如果中途停止，则不保存文件并删除半成品)
        if cancelled or should_stop(stop_event):
            if feedback is not None:
                feedback.set_activity("任务已停止", stop_reason="")
            outcome.status = RunStatus.CANCELLED
            outcome.errors.append(RunError(code="RUN_CANCELLED", message="博主采集任务被用户中止"))
            # 统计计费：全部未执行的单元都记为 skipped_count
            outcome.stats.skipped_count = len(profile_urls) - outcome.stats.success_count - outcome.stats.failed_count
            
            # 删除未完成的临时或已创建 XLSX
            Path(final_xlsx_path).unlink(missing_ok=True)
            
            outcome.save_to_json(final_report_path)
            outcome.artifacts.append(ArtifactRef(path=str(final_report_path), label="任务执行报告"))
            finish_callback(outcome)
            return outcome

        # 主动落盘
        if feedback is not None:
            feedback.set_activity(
                "正在保存博主信息 XLSX",
                stop_reason="正在等待工作簿保存完成。",
            )
        writer.save()
        if feedback is not None:
            feedback.advance(1, activity="博主信息导出完成", stop_reason="")

        # 根据最终统计更新终态
        if outcome.stats.success_count == 0:
            outcome.status = RunStatus.FAILED
            outcome.errors.append(RunError(code="not_found", message="没有找到任何有效的博主信息"))
            Path(final_xlsx_path).unlink(missing_ok=True)
        elif outcome.stats.failed_count > 0:
            outcome.status = RunStatus.PARTIAL
            outcome.output_path = str(final_xlsx_path)
            outcome.artifacts.append(ArtifactRef(path=str(final_xlsx_path), label="YouTube博主信息数据"))
        else:
            outcome.status = RunStatus.SUCCEEDED
            outcome.output_path = str(final_xlsx_path)
            outcome.artifacts.append(ArtifactRef(path=str(final_xlsx_path), label="YouTube博主信息数据"))

        outcome.save_to_json(final_report_path)
        outcome.artifacts.append(ArtifactRef(path=str(final_report_path), label="任务执行报告"))
        log_line(log_callback, f"完成，执行报告与结果已保存，RunID: {actual_run_id}")

    except Exception as exc:
        outcome.status = RunStatus.FAILED
        outcome.errors.append(RunError(code="unknown", message=str(exc)))
        outcome.save_to_json(final_report_path)
        log_error(log_callback, f"运行发生异常崩溃：{exc}")

    finish_callback(outcome)
    return outcome
