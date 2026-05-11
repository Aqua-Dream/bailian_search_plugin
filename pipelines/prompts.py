"""LLM 与百炼输入提示模板（纯函数）。"""

from __future__ import annotations

import textwrap
import time


def _identity_header(bot_name: str) -> str:
    name = (bot_name or "机器人").strip() or "机器人"
    time_now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    return f"你的名字是{name}。现在是{time_now}。"


def build_bailian_web_search_input(*, bot_name: str, question: str, context: str) -> str:
    """构造发往百炼 Responses API（含 web_search 工具）的用户输入文本。"""
    header = _identity_header(bot_name)
    blocks = [
        header,
        "",
        "请使用联网搜索工具检索最新公开信息，用简体中文、条理清晰地回答。引用事实时请隐含依据搜索结果，勿编造。",
        "",
    ]
    ctx = (context or "").strip()
    if ctx:
        blocks.extend(["【近期群内对话摘要】", ctx, ""])
    blocks.extend(["【用户问题】", (question or "").strip()])
    return "\n".join(blocks).strip()


def build_url_summarize_prompt(*, bot_name: str, url: str, content: str) -> str:
    """构建 URL 直访总结 prompt"""
    truncated_content = (content or "")[:8000]
    return textwrap.dedent(
        f"""
        {_identity_header(bot_name)}
        [任务]
        你是一个专业的内容总结专家。用户提供了一个网页链接，你的任务是阅读这个网页的内容，并提供一个全面、准确、结构清晰的总结。

        [网页URL]
        {url}

        [网页内容]
        {truncated_content}

        [要求]
        1. 提供网页的主要内容概述
        2. 如果是文章，总结其核心观点和关键信息
        3. 如果是产品页面，说明产品的主要特性和用途
        4. 如果是新闻，说明事件的关键要素（何时、何地、何人、何事、为何）
        5. 保持客观中立，不要添加主观评价
        6. 使用清晰的结构和层次组织信息
        7. 不要因为发布时间较新就认为内容是虚构的，请按当前时间理解信息
        8. 如果内容过于简短或无实质信息，请说明

        [你的总结]
        """
    ).strip()
