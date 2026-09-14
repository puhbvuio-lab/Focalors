import re
import random
from datetime import datetime

from src.core import (
    MultiSheetXlsxWriter,
    connect_existing_chromium,
    interruptible_sleep,
    should_stop,
    wait_if_paused,
    DEFAULT_X_CDP_URL,
    build_output_path,
)
from src.platforms.facebook.profile_works import (
    log_error,
    log_line,
    log_warn,
    parse_date_range,
    parse_fb_time_string,
    in_date_range,
    row_from_post,
    collect_profile_urls,
    parse_deep_post,
    PAGE_TIMEOUT_MS,
    SCROLL_DELAY_MS,
    NO_NEW_LIMIT,
)
from playwright.sync_api import sync_playwright

def _get_output_path(keyword: str) -> str:
    safe_keyword = re.sub(r'[\\/*?:"<>|]', "_", keyword)
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    return build_output_path("facebook", f"facebook_search_{safe_keyword}_{date_str}.xlsx", channel="search")

def run_facebook_keyword_search_spider(
    keywords_text: str, 
    limit_time_str: str, 
    start_date_str: str, 
    end_date_str: str, 
    sort_recent_str: str,
    log_callback, 
    finish_callback,
    stop_event, 
    pause_event, 
    **config
) -> None:
    keywords = [k.strip() for k in keywords_text.splitlines() if k.strip()]
    if not keywords:
        log_warn(log_callback, "未提供任何搜索关键词")
        if finish_callback:
            finish_callback(None)
        return
    
    limit_time_bool = (limit_time_str == "是")
    sort_recent_bool = (sort_recent_str == "是")
    start_dt = None
    end_dt = None
    if limit_time_bool:
        start_dt, end_dt = parse_date_range(start_date_str, end_date_str)
    
    page_timeout = int(config.get("page_load_timeout", PAGE_TIMEOUT_MS))
    scroll_delay_val = int(config.get("scroll_delay", SCROLL_DELAY_MS))
    no_new_limit = int(config.get("no_new_scroll_limit", NO_NEW_LIMIT))
    max_scrolls = int(config.get("max_scrolls", 200))
    max_posts = int(config.get("max_posts", 100))
    collect_comments_bool = (config.get("collect_comments", "否") == "是")
    cooldown_min_val = float(config.get("cooldown_min", 1.0))
    cooldown_max_val = float(config.get("cooldown_max", 3.0))
    scroll_px = int(config.get("scroll_px", 800))
    comment_top_limit = int(config.get("comment_top_limit", 100))
    
    browser = None
    playwright_context = None
    page = None
    try:
        with sync_playwright() as p:
            try:
                browser, playwright_context = connect_existing_chromium(p, DEFAULT_X_CDP_URL, log_callback=log_callback)
                if not browser:
                    log_error(log_callback, "无法连接到本地浏览器，请确保以调试模式启动 Chrome。")
                    return
                    
                page = playwright_context.new_page()
            
                for keyword_index, keyword in enumerate(keywords, 1):
                    if should_stop(stop_event):
                        break
                    
                    log_line(log_callback, f"[{keyword_index}/{len(keywords)}] 搜索关键词：{keyword}")
                    
                    # 阶段一：打开搜索页面并收集帖子链接
                    search_url = f"https://www.facebook.com/search/top?q={keyword}"
                    log_line(log_callback, f"阶段一：正在打开搜索页 - {search_url}")
                    page.goto(search_url, timeout=page_timeout)
                    page.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(3000)
                    
                    # 处理“近期排序”开关
                    if sort_recent_bool:
                        log_line(log_callback, "  > 用户开启了'近期排序'，尝试切换...")
                        try:
                            recent_switch = page.locator(
                                'input[role="switch"][aria-label*="近期"], input[role="switch"][aria-label*="最新"], input[role="switch"][aria-label*="Recent"]'
                            ).first
                            
                            if recent_switch.count() > 0:
                                is_checked = recent_switch.get_attribute("aria-checked")
                                if is_checked != "true":
                                    recent_switch.evaluate("node => node.click()")
                                    log_line(log_callback, "  > 成功触发 '近期/最新' 开关，等待页面刷新...")
                                    page.wait_for_timeout(4000)
                                else:
                                    log_line(log_callback, "  > 近期排序已处于开启状态。")
                            else:
                                log_line(log_callback, "  > 未找到 input 开关，尝试备用文字点击...")
                                fallback_switch = page.locator('div[role="switch"]').filter(
                                    has_text=re.compile(r"近期|最新|Recent", re.IGNORECASE)
                                ).first
                                if fallback_switch.count() > 0:
                                    fallback_switch.click(force=True)
                                    log_line(log_callback, "  > 成功通过备用开关切换，等待页面刷新...")
                                    page.wait_for_timeout(4000)
                        except Exception as e:
                            log_warn(log_callback, f"  [!] 切换排序时出错: {e}")
                    else:
                        log_line(log_callback, "  > 保持默认的搜索相关性排序。")
                    
                    # 收集链接（直接复用博主的 collect_profile_urls 方法）
                    collected_urls = collect_profile_urls(
                        page,
                        search_url,
                        max_scrolls,
                        limit_time_bool,
                        start_dt,
                        end_dt,
                        log_callback,
                        stop_event,
                        pause_event,
                        scroll_delay_val,
                        no_new_limit,
                        max_posts,
                        skip_navigation=True,
                        scroll_px=scroll_px,
                    )
                    
                    log_line(log_callback, f"收集完毕，共抓取到 {len(collected_urls)} 个帖子链接。准备进入详情页深度解析...")
                    
                    output_path = _get_output_path(keyword)
                    fieldnames = ["序号", "主页链接", "帖子链接", "发布时间", "帖子内容", "点赞数", "评论数", "分享数", "类型"]
                    
                    # 配置 Sheets
                    sheets_fields = {"帖子内容": fieldnames}
                    if collect_comments_bool:
                        comment_fieldnames = ["原帖链接", "评论内容", "抓取时间", "是否主楼"]
                        sheets_fields["评论详情"] = comment_fieldnames
                        
                    writer = MultiSheetXlsxWriter(output_path, sheets_fields)
                    
                    total_written = 0
                    comments_written = 0
                    
                    for idx, post_url in enumerate(collected_urls):
                        if should_stop(stop_event): 
                            break
                        if wait_if_paused(pause_event, stop_event): 
                            break
                        
                        try:
                            log_line(log_callback, f"抓取详情 [{idx+1}/{len(collected_urls)}]: {post_url}")
                            post_data = parse_deep_post(page, post_url, collect_comments=collect_comments_bool, max_comments=comment_top_limit)
    
                            # 精确时间过滤
                            if limit_time_bool and start_dt and end_dt:
                                pub_dt = parse_fb_time_string(post_data.get("published_at", ""))
                                if pub_dt and not in_date_range(pub_dt, start_dt, end_dt):
                                    log_line(log_callback, f"  剔除: 精确时间 {pub_dt.date()} 不在范围内")
                                    continue
                                if pub_dt:
                                    post_data["published_at"] = pub_dt.strftime("%Y-%m-%d %H:%M:%S")
    
                            row = row_from_post(total_written + 1, post_data, f"Keyword: {keyword}")
                            writer.writerow("帖子内容", row)
                            total_written += 1
    
                            # 评论已在 parse_deep_post 内部提取
                            if collect_comments_bool:
                                for c_row in post_data.get("comment_list", []):
                                    writer.writerow("评论详情", c_row)
                                    comments_written += 1
                                if post_data.get("comment_list"):
                                    log_line(log_callback, f"  成功提取到 {len(post_data['comment_list'])} 条评论。")
                                        
                            # 冷却时间
                            delay = random.uniform(cooldown_min_val, cooldown_max_val)
                            interruptible_sleep(delay, stop_event)
                            
                        except Exception as e:
                            log_error(log_callback, f"  详情解析失败: {e}")
                            
                    writer.save()
                    msg = f"完成关键词 {keyword}，有效导出 {total_written} 条帖子数据。"
                    if collect_comments_bool:
                        msg += f" 导出评论 {comments_written} 条。"
                    log_line(log_callback, msg)
                    if finish_callback:
                        finish_callback(output_path)
            finally:
                if page:
                    try:
                        page.close()
                    except Exception:
                        pass
                if playwright_context:
                    try:
                        playwright_context.close()
                    except Exception:
                        pass
                if browser:
                    try:
                        browser.close()
                    except Exception:
                        pass
    except Exception as e:
        log_error(log_callback, f"运行异常: {e}")
        raise e
    finally:
        if finish_callback:
            finish_callback(output_path)
