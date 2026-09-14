# agent_create.py
# 创建 AI Agent

from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from .config import config


SYSTEM_PROMPT = """
你是视频/推文标题判断助手。输入是二维数组：[序号, 标题]。
请根据每条标题判断标题所描述的视频或推文内容是否是 AI 生成内容，并补充标题文本的主要语言。

输出必须是标准 JSON 二维数组：
[
  [序号, "视频标题", "是/否", "主要语言"]
]

规则：
1. "是否AIGC" 判断的是标题所指向的视频或推文内容是否为 AI 生成、AI 改编、AI 动画、AI 实写化、AI cover、AI art、AI generated、Stable Diffusion、Midjourney、Sora、Runway、ComfyUI、LoRA、AIGC 等内容，不是判断标题文字本身是不是 AI 写的。
2. 如果标题明确表示视频或推文内容由 AI 生成、使用 AI 制作、AI 改编、AI 实写化、AI 动画化、AI 配音/翻唱/cover 等，判断为 "是"。
3. 如果标题只是普通视频或普通推文标题，没有明确 AI 生成或 AI 制作含义，判断为 "否"。
4. "主要语言" 只判断标题文本的主要语言，可选：中文、英语、日语、韩语、西班牙语、泰语、俄语、阿拉伯语、印地语、越南语、法语、德语、葡萄牙语、印尼语、意大利语、土耳其语、混合语言、未知。
5. 保持序号、标题、顺序不变。
6. 不要新增、删除、合并任何数据。
7. 只返回 JSON，不要解释，不要 Markdown，不要代码块。
"""


def get_agent():
    if not config.API_KEY:
        raise ValueError(
            "未检测到环境变量 DEEPSEEK_API_KEY 或 API_KEY，请先设置 DeepSeek API Key。"
        )

    llm = ChatOpenAI(
        model=config.MODEL_NAME,
        api_key=config.API_KEY,
        base_url=config.BASE_URL,
        temperature=config.TEMPERATURE,
        max_tokens=config.MAX_TOKENS,
        request_timeout=120,
        max_retries=3,
    )

    agent = create_agent(
        model=llm,
        system_prompt=SYSTEM_PROMPT,
    )

    return agent
