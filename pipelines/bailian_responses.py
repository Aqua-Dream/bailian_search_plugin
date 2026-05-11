"""阿里云百炼 OpenAI 兼容 Responses API：联网搜索与文搜图。

文档：
- 联网搜索：https://help.aliyun.com/zh/model-studio/web-search
- 文搜图：https://help.aliyun.com/zh/model-studio/web-search-image
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from openai import APIError, APITimeoutError, AsyncOpenAI

logger = logging.getLogger(__name__)


def _item_type(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("type") or "")
    return str(getattr(item, "type", "") or "")


def _item_field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def extract_responses_output_text(response: Any) -> str:
    """从 Responses 对象取出模型最终文本（兼容 output_text 与 message 块）。"""
    direct = getattr(response, "output_text", None)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    parts: list[str] = []
    for item in getattr(response, "output", None) or []:
        if _item_type(item) != "message":
            continue
        content = _item_field(item, "content") or []
        for block in content:
            bt = _item_type(block)
            if bt in {"output_text", "text"}:
                text = _item_field(block, "text")
                if isinstance(text, str) and text:
                    parts.append(text)
    return "\n".join(parts).strip()


def extract_web_search_image_items(response: Any) -> list[dict[str, Any]]:
    """解析 ``web_search_image_call`` 输出为统一图片条目列表。"""
    results: list[dict[str, Any]] = []
    for item in getattr(response, "output", None) or []:
        if _item_type(item) != "web_search_image_call":
            continue
        raw_out = _item_field(item, "output")
        images_payload: Any = None
        if isinstance(raw_out, str) and raw_out.strip():
            try:
                images_payload = json.loads(raw_out)
            except json.JSONDecodeError:
                logger.warning("文搜图 output 非合法 JSON，跳过该项")
                continue
        elif isinstance(raw_out, list):
            images_payload = raw_out
        if not isinstance(images_payload, list):
            continue
        for img in images_payload:
            if not isinstance(img, dict):
                continue
            url = img.get("url") or img.get("image") or img.get("image_url")
            if not url or not isinstance(url, str):
                continue
            title = img.get("title")
            results.append(
                {
                    "image": url.strip(),
                    "title": str(title).strip() if title else "",
                    "thumbnail": str(img.get("thumbnail") or url).strip(),
                    "url": str(img.get("page_url") or img.get("link") or "").strip(),
                    "source": str(img.get("source") or "").strip(),
                }
            )
    return results


class BailianResponsesClient:
    """百炼 Responses API 封装（联网搜索 / 文搜图）。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float,
    ) -> None:
        self._model = model
        self._timeout = timeout_seconds
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
        )

    async def web_search_answer(self, user_input: str) -> str:
        """启用 ``web_search`` 工具，返回模型整理后的简体中文回答。"""
        logger.info("[bailian] web_search 请求 model=%s input_len=%d", self._model, len(user_input))
        try:
            response = await self._client.responses.create(
                model=self._model,
                input=user_input,
                tools=[{"type": "web_search"}],
                timeout=self._timeout,
            )
        except APITimeoutError as exc:
            logger.warning("百炼联网搜索超时: %s", exc)
            return "联网搜索请求超时，请稍后再试。"
        except APIError as exc:
            logger.warning("百炼联网搜索 API 错误: %s", exc)
            return f"联网搜索服务暂时不可用：{exc}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("百炼联网搜索异常: %s", exc)
            return f"联网搜索调用异常：{exc}"

        text = extract_responses_output_text(response)
        if text:
            return text
        logger.warning("百炼返回空文本 output=%r", getattr(response, "output", None))
        return "联网搜索已完成，但未返回可用正文，请换个问法或稍后再试。"

    async def web_search_images(self, user_input: str) -> list[dict[str, Any]]:
        """启用 ``web_search_image`` 工具，返回图片 URL 列表（结构化字典）。"""
        logger.info("[bailian] web_search_image 请求 model=%s input_len=%d", self._model, len(user_input))
        try:
            response = await self._client.responses.create(
                model=self._model,
                input=user_input,
                tools=[{"type": "web_search_image"}],
                timeout=self._timeout,
            )
        except APITimeoutError as exc:
            logger.warning("百炼文搜图超时: %s", exc)
            return []
        except APIError as exc:
            logger.warning("百炼文搜图 API 错误: %s", exc)
            return []
        except Exception as exc:  # noqa: BLE001
            logger.exception("百炼文搜图异常: %s", exc)
            return []

        items = extract_web_search_image_items(response)
        logger.info("[bailian] web_search_image 解析到 %d 条图片", len(items))
        return items


def resolve_dashscope_api_key(explicit: str) -> Optional[str]:
    """配置中的 api_key 优先，否则读环境变量 ``DASHSCOPE_API_KEY``。"""
    key = (explicit or "").strip()
    if key:
        return key
    env_key = (os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    return env_key or None
