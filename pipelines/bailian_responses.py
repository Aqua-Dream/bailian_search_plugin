"""阿里云百炼 OpenAI 兼容 Responses API：联网搜索与文搜图。

文档：
- 联网搜索：https://help.aliyun.com/zh/model-studio/web-search
- 文搜图：https://help.aliyun.com/zh/model-studio/web-search-image
"""

from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace
from typing import Any, Optional

import httpx
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


def _parse_web_search_image_raw_output(raw_out: Any) -> list[dict[str, Any]]:
    """将文搜图工具 ``output`` 字段解析为统一图片条目列表。"""
    images_payload: Any = None
    if isinstance(raw_out, str) and raw_out.strip():
        try:
            images_payload = json.loads(raw_out)
        except json.JSONDecodeError:
            logger.warning("文搜图 output 非合法 JSON，跳过该项")
            return []
    elif isinstance(raw_out, list):
        images_payload = raw_out
    else:
        return []
    if not isinstance(images_payload, list):
        return []

    results: list[dict[str, Any]] = []
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


def _images_from_web_search_image_output_item(item: Any) -> list[dict[str, Any]]:
    if _item_type(item) != "web_search_image_call":
        return []
    return _parse_web_search_image_raw_output(_item_field(item, "output"))


def extract_web_search_image_items(response: Any) -> list[dict[str, Any]]:
    """解析 ``web_search_image_call`` 输出为统一图片条目列表。"""
    results: list[dict[str, Any]] = []
    for item in getattr(response, "output", None) or []:
        results.extend(_images_from_web_search_image_output_item(item))
    return results


def _as_response_like(resp: Any) -> Any:
    """将流式 ``response.completed`` 中的 ``response`` 载荷转成可供 ``extract_web_search_image_items`` 使用的对象。"""
    if resp is None:
        return SimpleNamespace(output=[])
    if isinstance(resp, dict):
        return SimpleNamespace(output=resp.get("output") or [])
    return resp


def _streaming_event_type(event: Any) -> str:
    """流式事件类型：须用属性读，勿依赖整包 ``model_dump``（百炼 ``web_search_image_call`` 可能不在 SDK 联合类型里，dump 会丢 ``item.output``）。"""
    return str(getattr(event, "type", None) or "")


def _streaming_response_completed_response(event: Any) -> Any:
    return getattr(event, "response", None)


def _streaming_output_item_done_item(event: Any) -> Any:
    """``response.output_item.done`` 上的 ``item``（与官方流式示例一致，直接取 ``event.item``）。"""
    return getattr(event, "item", None)


def _image_items_from_output_item(raw_item: Any) -> list[dict[str, Any]]:
    """从文搜图 ``output_item`` 解析图片列表：先属性路径，再尝试 ``model_dump``（兼容 SDK 包装）。"""
    found = _images_from_web_search_image_output_item(raw_item)
    if found:
        return found
    if raw_item is None or isinstance(raw_item, dict):
        return []
    md = getattr(raw_item, "model_dump", None)
    if not callable(md):
        return []
    try:
        dumped = md(mode="json")
    except (TypeError, ValueError):
        dumped = md()
    if isinstance(dumped, dict):
        return _images_from_web_search_image_output_item(dumped)
    return []


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

    async def _web_search_images_blocking(self, user_input: str) -> list[dict[str, Any]]:
        response = await self._client.responses.create(
            model=self._model,
            input=user_input,
            tools=[{"type": "web_search_image"}],
            timeout=self._timeout,
        )
        return extract_web_search_image_items(response)

    async def _web_search_images_streaming(
        self,
        user_input: str,
        *,
        early_stop_items: int,
    ) -> list[dict[str, Any]]:
        """流式消费 Responses，在集齐 ``early_stop_items`` 条图片后提前结束，减少等待模型后续输出的时间。

        百炼文搜图与官方文档一致（见 ``response.output_item.done`` + ``item.output``）。
        解析时**禁止**依赖整事件的 ``model_dump``：``web_search_image_call`` 可能不在当前 OpenAI SDK
        的 ``ResponseOutputItem`` 联合类型中，序列化会丢失 ``output``，表现为「流式完全无图」。
        """
        merged: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        # 文搜图检索阶段可能长时间无 SSE 字节；read 须明显大于单次「总超时」浮点值，否则会 httpx.ReadTimeout
        total = float(self._timeout)
        stream_timeout = httpx.Timeout(
            connect=min(20.0, total),
            read=max(total * 3.0, 180.0),
            write=min(60.0, total),
            pool=10.0,
        )

        stream = await self._client.responses.create(
            model=self._model,
            input=user_input,
            tools=[{"type": "web_search_image"}],
            timeout=stream_timeout,
            stream=True,
        )
        try:
            async for event in stream:
                typ = _streaming_event_type(event)
                if typ == "response.output_item.done":
                    raw_item = _streaming_output_item_done_item(event)
                    for it in _image_items_from_output_item(raw_item):
                        u = (it.get("image") or "").strip()
                        if u and u not in seen_urls:
                            seen_urls.add(u)
                            merged.append(it)
                        if len(merged) >= early_stop_items:
                            break
                    if len(merged) >= early_stop_items:
                        break
                elif typ == "response.completed":
                    response_obj = _streaming_response_completed_response(event)
                    response_like = _as_response_like(response_obj)
                    for it in extract_web_search_image_items(response_like):
                        u = (it.get("image") or "").strip()
                        if u and u not in seen_urls:
                            seen_urls.add(u)
                            merged.append(it)
                        if len(merged) >= early_stop_items:
                            break
                    break
        finally:
            await stream.close()

        return merged

    async def web_search_images(
        self,
        user_input: str,
        *,
        early_stop_items: int = 5,
    ) -> list[dict[str, Any]]:
        """启用 ``web_search_image`` 工具，返回图片 URL 列表（结构化字典）。

        默认走 **流式**：在 SSE 中一旦出现 ``web_search_image_call`` 的完整 ``output``，解析出前若干条后即
        **断开连接**，不再等待模型生成后续说明文字，以缩短总耗时、降低超时概率。若流式不可用或
        未解析到条目，则回退为一次性非流式请求。
        """
        stop_at = max(1, int(early_stop_items))
        logger.info(
            "[bailian] web_search_image 请求 model=%s input_len=%d stream_early_stop=%d",
            self._model,
            len(user_input),
            stop_at,
        )

        try:
            items = await self._web_search_images_streaming(user_input, early_stop_items=stop_at)
        except httpx.ReadTimeout as exc:
            logger.warning("百炼文搜图流式 read 超时（检索阶段 SSE 间隔过长）: %s，将尝试非流式", exc)
            items = []
        except httpx.TimeoutException as exc:
            logger.warning("百炼文搜图流式 HTTP 超时: %s，将尝试非流式", exc)
            items = []
        except APITimeoutError as exc:
            logger.warning("百炼文搜图流式超时: %s", exc)
            items = []
        except APIError as exc:
            logger.warning("百炼文搜图流式 API 错误: %s", exc)
            items = []
        except Exception as exc:  # noqa: BLE001
            logger.warning("百炼文搜图流式异常，将尝试非流式: %s", exc, exc_info=True)
            items = []

        if items:
            logger.info("[bailian] web_search_image 流式解析到 %d 条图片", len(items))
            return items

        try:
            items = await self._web_search_images_blocking(user_input)
        except APITimeoutError as exc:
            logger.warning("百炼文搜图非流式超时: %s", exc)
            return []
        except APIError as exc:
            logger.warning("百炼文搜图非流式 API 错误: %s", exc)
            return []
        except Exception as exc:  # noqa: BLE001
            logger.exception("百炼文搜图非流式异常: %s", exc)
            return []

        logger.info("[bailian] web_search_image 非流式解析到 %d 条图片", len(items))
        return items


def resolve_dashscope_api_key(explicit: str) -> Optional[str]:
    """配置中的 api_key 优先，否则读环境变量 ``DASHSCOPE_API_KEY``。"""
    key = (explicit or "").strip()
    if key:
        return key
    env_key = (os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    return env_key or None
