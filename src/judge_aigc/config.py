# config.py
# 项目配置文件

import os
import dotenv
from dotenv import find_dotenv

class Config:

    dotenv.load_dotenv(find_dotenv())
    # ===== DeepSeek / OpenAI Compatible API 配置 =====
    API_KEY = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("API_KEY", "")
    BASE_URL = os.environ.get("DEEPSEEK_BASE_URL") or os.environ.get("BASE_URL", "https://api.deepseek.com")
    MODEL_NAME = os.environ.get("DEEPSEEK_MODEL_NAME") or os.environ.get("MODEL_NAME", "deepseek-chat")

    # ===== 模型参数 =====
    TEMPERATURE = 0.1
    MAX_TOKENS = 8192

    # ===== 文件路径 =====
    INPUT_TXT_PATH = "./n.txt"
    OUTPUT_EXCEL_PATH = "AI判断结果.xlsx"

    # ===== 读取批次配置 =====
    # 不建议太大，500 容易导致 JSON 输出被截断
    # 推荐 50~150
    ROW_LIMIT = 100

    # ===== Excel 配置 =====
    SHEET_NAME = "AIGC和语言判断"
    HEADERS = ["序号", "视频标题(推文内容)", "是否AIGC", "主要语言"]

    # ===== 运行配置 =====
    SAVE_EVERY_BATCHES = 5
    SLEEP_SECONDS = 0.5

    # 本地只直接确认明显 AIGC 的标题；普通/不确定标题交给 AI 判断。
    TRUST_LOCAL_NEGATIVE_AIGC = False


config = Config()
