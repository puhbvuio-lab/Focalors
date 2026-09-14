import pandas as pd
import numpy as np
from pathlib import Path

from src.core.output import build_output_path
from src.core.timing import should_stop
from src.core import log_error, log_line, log_warn


def run_anomaly_detection(values, config, log_callback, finish_callback, stop_event, pause_event=None):
    """
    执行数据异常分析检测的主逻辑。
    """
    input_file = values.get("input_xlsx")
    if not input_file or not Path(input_file).is_file():
        log_warn(log_callback, "输入文件不存在或无效")
        finish_callback(None)
        return

    # 获取配置阈值
    high_view_threshold = int(config.get("high_view_threshold", 1000))
    high_like_threshold = int(config.get("high_like_threshold", 50))
    abnormal_ratio_multiplier = float(config.get("abnormal_ratio_multiplier", 2.0))
    abnormal_ratio_min_trigger = int(config.get("abnormal_ratio_min_trigger", 5))
    strict_zero_check = config.get("strict_zero_check", True)

    log_line(log_callback, f"加载数据：{input_file}")
    if should_stop(stop_event):
        return
        
    try:
        df = pd.read_excel(input_file)
    except Exception as e:
        log_error(log_callback, f"读取 Excel 失败: {e}")
        finish_callback(None)
        return

    metrics = ['浏览量', '点赞量', '评论数', '转发量']

    # 记录NA状态并将NA转换为0进行数学运算
    for m in metrics:
        if m in df.columns:
            df[f'is_na_{m}'] = df[m].isna()
            df[m] = pd.to_numeric(df[m], errors='coerce').fillna(0)
        else:
            df[f'is_na_{m}'] = True
            df[m] = 0

    log_line(log_callback, "开始执行异常检测判定...")

    def evaluate_row(row):
        reasons = []
        
        views = row.get('浏览量', 0)
        likes = row.get('点赞量', 0)
        forwards = row.get('转发量', 0)
        
        na_v = row.get('is_na_浏览量', True)
        na_l = row.get('is_na_点赞量', True)
        na_f = row.get('is_na_转发量', True)
        
        # 1. 浏览量倒挂
        if not na_v:
            if (not na_l) and views < likes:
                reasons.append('点赞量大于浏览量')
            if (not na_f) and views < forwards:
                reasons.append('转发量大于浏览量')
                
        # 2. 绝对0值的逻辑矛盾
        if strict_zero_check:
            if (not na_v) and views == 0:
                if (not na_l) and likes > 0:
                    reasons.append('浏览为0但有点赞')
                if (not na_f) and forwards > 0:
                    reasons.append('浏览为0但有转发')
                    
            if (not na_l) and likes == 0:
                if (not na_f) and forwards > 0:
                    reasons.append('点赞为0但有转发')

        # 3. 高热度下的数据缺失
        if views >= high_view_threshold or likes >= high_like_threshold:
            if (not na_f) and forwards == 0:
                reasons.append('高播放/高点赞但转发为0')
                
        # 4. 派生指标比例严重失调
        if (not na_f) and (not na_l) and forwards > 0:
            if forwards > (likes * abnormal_ratio_multiplier) and forwards > abnormal_ratio_min_trigger:
                reasons.append(f'转发反常高于点赞(超{abnormal_ratio_multiplier}倍)')
                
        if len(reasons) > 0:
            return '异常', ' | '.join(reasons)
            
        if na_v and na_l and na_f and row.get('is_na_评论数', True):
            return '无数据', '热度字段全为空(NA)'
            
        return '正常', '无'

    if should_stop(stop_event):
        return

    # DataFrame apply 计算
    res = df.apply(evaluate_row, axis=1)
    df['data_status'] = [x[0] for x in res]
    df['异常原因'] = [x[1] for x in res]
    
    # 恢复原始空值 (NA)
    for m in metrics:
        if m in df.columns:
            is_na_series = df[f'is_na_{m}']
            df.loc[is_na_series, m] = np.nan
            
    df = df.drop(columns=[f'is_na_{m}' for m in metrics], errors='ignore')
    
    anomaly_count = len(df[df['data_status'] == '异常'])
    log_line(log_callback, f"判定完成，共发现 {anomaly_count} 条异常记录。")

    if should_stop(stop_event):
        return

    # 导出文件
    input_stem = Path(input_file).stem
    out_filename = f"{input_stem}_检测结果.xlsx"
    out_path = build_output_path("data", out_filename, channel="anomaly")
    
    log_line(log_callback, f"正在保存至：{out_path}")
    try:
        # 创建父目录
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        df.to_excel(out_path, index=False)
        finish_callback(out_path)
    except Exception as e:
        log_error(log_callback, f"保存 Excel 失败: {e}")
        finish_callback(None)
