from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

import dotenv
from dotenv import find_dotenv
from langchain_openai import ChatOpenAI

from src.core.config_store import GLOBAL_CONFIG_DEFAULTS, GLOBAL_TOOL_ID, load_config


@dataclass(frozen=True)
class AIProviderConfig:
    api_key: str
    model: str
    base_url: str | None = None


def _first_non_empty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def resolve_ai_provider_config(global_values: Mapping[str, Any] | None = None) -> AIProviderConfig:
    dotenv.load_dotenv(find_dotenv())

    if global_values is None:
        global_values = load_config(GLOBAL_TOOL_ID, GLOBAL_CONFIG_DEFAULTS, None)

    api_key = _first_non_empty(
        global_values.get("ai_api_key"),
        os.environ.get("OPENAI_API_KEY"),
        os.environ.get("DEEPSEEK_API_KEY"),
        os.environ.get("API_KEY"),
    )
    model = _first_non_empty(
        global_values.get("ai_model"),
        os.environ.get("OPENAI_MODEL"),
        os.environ.get("DEEPSEEK_MODEL_NAME"),
        os.environ.get("MODEL_NAME"),
        "deepseek-chat",
    )
    base_url = _first_non_empty(
        global_values.get("ai_base_url"),
        os.environ.get("OPENAI_BASE_URL"),
        os.environ.get("DEEPSEEK_BASE_URL"),
        os.environ.get("BASE_URL"),
    )

    return AIProviderConfig(
        api_key=api_key,
        model=model,
        base_url=base_url or None,
    )


def build_chat_openai(
    *,
    temperature: float = 0.1,
    max_tokens: int | None = None,
    request_timeout: float | None = 120,
    max_retries: int = 3,
    provider_config: AIProviderConfig | None = None,
) -> ChatOpenAI:
    provider = provider_config or resolve_ai_provider_config()
    if not provider.api_key:
        raise ValueError(
            "未检测到 AI API Key，请在全局配置中设置 AI API Key，"
            "或配置 OPENAI_API_KEY / DEEPSEEK_API_KEY / API_KEY。"
        )

    kwargs: dict[str, Any] = {
        "model": provider.model,
        "api_key": provider.api_key,
        "temperature": float(temperature),
        "max_retries": int(max_retries),
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = int(max_tokens)
    if request_timeout is not None:
        kwargs["request_timeout"] = request_timeout
    if provider.base_url:
        kwargs["base_url"] = provider.base_url
    return ChatOpenAI(**kwargs)
