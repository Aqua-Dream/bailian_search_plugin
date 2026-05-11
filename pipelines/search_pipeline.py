"""主搜索流程：聊天上下文 + 百炼 Responses API（web_search 工具）。"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Optional

from .bailian_responses import BailianResponsesClient
from ._envelope import peel_envelope
from .prompts import build_bailian_web_search_input

if TYPE_CHECKING:
    from maibot_sdk import PluginContext

    from ..config import ModelsSection

logger = logging.getLogger(__name__)

# 传入百炼联网搜索的「群内摘要」过长会显著增加 Token 与首包耗时；此处做硬截断（非配置项）。
_WEB_SEARCH_CONTEXT_MAX_CHARS = 6000


def _truncate_context_for_bailian(raw: str, *, max_chars: int) -> str:
    text = (raw or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 24].rstrip() + "\n…（上下文已截断，省略更早消息）"


class SearchPipeline:
    """百炼联网搜索流水线"""

    def __init__(
        self,
        ctx: "PluginContext",
        *,
        models_cfg: "ModelsSection",
        bailian: Optional[BailianResponsesClient],
    ) -> None:
        self._ctx = ctx
        self._models = models_cfg
        self._bailian = bailian

    async def run(
        self,
        question: str,
        *,
        chat_id: str,
        bot_name: str,
    ) -> str:
        if not self._bailian:
            return (
                "未配置百炼 API Key：请在插件配置 [bailian].api_key 填写，"
                "或设置环境变量 DASHSCOPE_API_KEY 后重试。"
            )

        context_str = await self._fetch_context(chat_id)
        context_for_bailian = _truncate_context_for_bailian(
            context_str,
            max_chars=_WEB_SEARCH_CONTEXT_MAX_CHARS,
        )
        user_input = build_bailian_web_search_input(
            bot_name=bot_name,
            question=question,
            context=context_for_bailian,
        )
        logger.info(
            "百炼联网搜索: question_len=%d context_raw_len=%d context_bailian_len=%d",
            len(question),
            len(context_str),
            len(context_for_bailian),
        )
        return await self._bailian.web_search_answer(user_input)

    async def _fetch_context(self, chat_id: str) -> str:
        """拉聊天上下文并拼成可读文本（与旧版逻辑一致）。"""
        if not chat_id:
            logger.info("_fetch_context: chat_id 为空,跳过")
            return ""

        time_gap = self._models.context_time_gap
        max_limit = self._models.context_max_limit
        current_ts = time.time()
        start_ts = current_ts - time_gap

        logger.info(
            "_fetch_context: chat_id=%s start_ts=%.3f end_ts=%.3f limit=%d",
            chat_id,
            start_ts,
            current_ts,
            max_limit,
        )

        try:
            messages = await self._ctx.message.get_by_time_in_chat(
                chat_id=chat_id,
                start_time=start_ts,
                end_time=current_ts,
                limit=max_limit,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_by_time_in_chat 失败: %s", exc)
            return ""

        messages = peel_envelope(messages)
        if isinstance(messages, dict):
            inner_list = messages.get("messages")
            if isinstance(inner_list, list):
                messages = inner_list
            else:
                logger.warning(
                    "_fetch_context: peel 后仍是 dict 且无 'messages' 列表,keys=%s",
                    sorted(messages.keys()),
                )
                return ""

        if not isinstance(messages, list):
            logger.warning("_fetch_context: messages 非 list,type=%s", type(messages).__name__)
            return ""

        if not messages:
            logger.info("_fetch_context: 拿到空列表(时间窗内可能没有消息)")
            return ""

        text = _format_messages_to_readable(messages)
        preview = text[:200].replace("\n", "\\n") if text else ""
        logger.info("_fetch_context: 拼出文本长度=%d preview=%r", len(text), preview)
        return text


def _format_messages_to_readable(messages: list) -> str:
    lines: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        user_info = (msg.get("message_info") or {}).get("user_info") or {}
        user_name = (
            user_info.get("user_cardname")
            or user_info.get("user_nickname")
            or user_info.get("user_id")
            or "未知"
        )
        text = msg.get("processed_plain_text") or msg.get("display_message") or ""
        if not text:
            continue
        ts_prefix = ""
        ts_raw = msg.get("timestamp")
        if ts_raw is not None:
            try:
                ts_float = float(ts_raw)
                ts_prefix = "[" + time.strftime("%H:%M:%S", time.localtime(ts_float)) + "] "
            except (ValueError, TypeError):
                ts_prefix = ""
        lines.append(f"{ts_prefix}{user_name}: {text}")
    return "\n".join(lines)
