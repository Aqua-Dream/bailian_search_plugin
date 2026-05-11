"""图片条目与查询词相关性工具（百炼文搜图返回后的本地排序/过滤）。"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


def build_image_query_keywords(query: str) -> list[str]:
    """从查询词提取用于匹配的片段。"""
    if not query:
        return []
    pieces: list[str] = []
    for token in re.split(r"\s+", query.lower().strip()):
        if not token:
            continue
        words = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", token)
        if words:
            pieces.extend(words)
        else:
            pieces.append(token)
    return [p for p in pieces if len(p) >= 2]


def image_item_relevance_score(item: dict[str, Any], keywords: list[str]) -> int:
    """统计关键词在标题、落地页、来源等字段中的命中次数（越多越相关）。"""
    if not keywords:
        return 0
    title = str(item.get("title") or "")
    page_url = str(item.get("url") or "")
    source = str(item.get("source") or "")
    thumbnail = str(item.get("thumbnail") or "")
    image_url = str(item.get("image") or "")
    path = ""
    try:
        path = urlparse(image_url).path.lower()
    except Exception:
        path = ""
    text = f"{title} {page_url} {source} {thumbnail} {path}".lower()
    return sum(1 for kw in keywords if kw.lower() in text)


def rank_and_filter_image_items(
    items: list[dict[str, Any]],
    query: str,
) -> list[dict[str, Any]]:
    """按相关性降序排列；若存在任意命中则丢弃完全无命中的结果。"""
    keywords = build_image_query_keywords(query)
    if not keywords:
        return items

    scored = [(image_item_relevance_score(it, keywords), i, it) for i, it in enumerate(items)]
    max_score = max((s for s, _, _ in scored), default=0)
    if max_score > 0:
        scored = [(s, i, it) for s, i, it in scored if s > 0]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [it for _, _, it in scored]
