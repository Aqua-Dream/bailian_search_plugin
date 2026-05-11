"""图片搜索流水线：百炼 Responses API 文搜图（web_search_image）+ 下载去重。"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections import deque
from typing import Any, Literal, Optional

import aiohttp

from .bailian_responses import BailianResponsesClient
from .image_query_utils import (
    build_image_query_keywords,
    image_item_relevance_score,
    rank_and_filter_image_items,
)

logger = logging.getLogger(__name__)

# 文搜图：百炼返回后至多采纳的候选条数（与历史去重队列尺度相关）
_MAX_IMAGE_CANDIDATES = 15
# 单次工具调用最多发送的图片张数（与 Tool 参数 n 上限一致）
_MAX_IMAGES_PER_REQUEST = 5

ImageSearchStatus = Literal["ok", "no_results", "no_unique", "all_failed"]

ENGINE_LABEL = "dashscope_web_search_image"


def _log_image_items_stage(
    stage: str,
    query: str,
    items: list[dict[str, Any]],
) -> None:
    """打印每条图片结果，便于对照关键词核查。"""
    kws = build_image_query_keywords(query)
    prefix = "[image_search]"
    if not items:
        logger.info("%s [%s] query=%r 条目数=0", prefix, stage, query)
        return
    logger.info(
        "%s [%s] query=%r 分词关键词=%r 条目数=%d",
        prefix,
        stage,
        query,
        kws,
        len(items),
    )
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            logger.info("%s [%s] #%d (非 dict,跳过) %r", prefix, stage, i, item)
            continue
        img = item.get("image") or ""
        thumb = item.get("thumbnail") or ""
        page = item.get("url") or ""
        title = (item.get("title") or "").replace("\n", " ").strip()
        source = item.get("source") or ""
        score = image_item_relevance_score(item, kws) if kws else 0
        logger.info(
            "%s [%s] #%d relevance_score=%d image=%s",
            prefix,
            stage,
            i,
            score,
            img,
        )
        logger.info("%s [%s] #%d thumbnail=%s", prefix, stage, i, thumb)
        logger.info("%s [%s] #%d page_url=%s", prefix, stage, i, page)
        logger.info("%s [%s] #%d source=%s title=%s", prefix, stage, i, source, title[:200])


class ImageSearchPipeline:
    """文搜图流水线"""

    def __init__(
        self,
        *,
        bailian: Optional[BailianResponsesClient],
    ) -> None:
        self._bailian = bailian

        max_results = max(_MAX_IMAGE_CANDIDATES, 0)
        self._image_history: dict[str, deque[tuple[str, float]]] = {}
        self._image_history_max_size: int = max(30, max_results * 3) if max_results > 0 else 30
        self._image_repeat_window_seconds: int = 30 * 60
        self._max_distinct_queries: int = 200

    def _evict_stale_queries(self, now: float) -> None:
        window = self._image_repeat_window_seconds
        stale = [
            q
            for q, hist in self._image_history.items()
            if not hist or now - hist[-1][1] > window
        ]
        for q in stale:
            self._image_history.pop(q, None)
        if len(self._image_history) > self._max_distinct_queries:
            sorted_by_age = sorted(
                self._image_history.items(),
                key=lambda kv: kv[1][-1][1] if kv[1] else 0.0,
            )
            for q, _ in sorted_by_age[: -self._max_distinct_queries]:
                self._image_history.pop(q, None)

    async def find_images_b64(
        self,
        query: str,
        count: int,
    ) -> tuple[ImageSearchStatus, list[tuple[str, str]]]:
        """按检索意图文搜图（``query`` 为自然语言或短语均可），下载最多 ``count`` 张（调用方保证 1<=count<=5）。"""
        if not self._bailian:
            logger.warning("[image_search] 未配置百炼 API Key，跳过文搜图")
            return ("no_results", [])

        count = min(_MAX_IMAGES_PER_REQUEST, max(1, int(count)))

        limit = max(min(_MAX_IMAGE_CANDIDATES, 50), count)
        # 百炼侧文搜图常见一次返回约 30 张，会拉长 Responses 耗时与 Token；无官方「张数」参数，
        # 仅在自然语言中约束检索范围，并声明下游只需少量候选（仍可能返回较多，由本地 limit 截断）。
        user_input = (
            "请使用文搜图工具，根据下列检索需求在互联网检索相关图片。\n"
            "检索需求可为完整自然语言（含角色、作品、风格、用途如壁纸/表情包等），不必压缩为几个词。\n"
            f"检索需求：{query}\n"
            "以语义相关为准，可包含表情包、同人图、截图等。\n"
            "\n"
            "【重要】请尽量只检索少量最相关的图片（建议不超过 10 张）；"
            f"本插件随后需从中选用最多 {count} 张发送给用户，过多候选会显著拖慢响应。"
        )
        logger.info(
            "[image_search][bailian_try] query=%r max_items=%d want_count=%d",
            query,
            limit,
            count,
        )

        image_results = await self._bailian.web_search_images(user_input)
        image_results = image_results[:limit]

        _log_image_items_stage("bailian_raw", query, image_results)
        image_results = rank_and_filter_image_items(image_results, query)
        _log_image_items_stage("after_rank_filter", query, image_results)

        if not image_results:
            return ("no_results", [])

        image_urls: list[str] = []
        seen: set[str] = set()
        for item in image_results:
            url = item.get("image") if isinstance(item, dict) else None
            if not url or url in seen:
                continue
            seen.add(url)
            image_urls.append(url)

        logger.info(
            "[image_search][urls_dedup] query=%r 去重后条数=%d 全部 image URL:\n%s",
            query,
            len(image_urls),
            "\n".join(f"  [{i}] {u}" for i, u in enumerate(image_urls)),
        )
        if not image_urls:
            return ("no_results", [])

        history = self._image_history.get(query)
        if history is None:
            self._evict_stale_queries(time.time())
            history = deque(maxlen=self._image_history_max_size)
            self._image_history[query] = history

        now = time.time()
        recent_urls = {
            url
            for url, ts in history
            if now - ts < self._image_repeat_window_seconds
        }
        candidates = [u for u in image_urls if u not in recent_urls]
        logger.info(
            "[image_search][candidates] query=%r 排除近30分钟已发后条数=%d URL 列表:\n%s",
            query,
            len(candidates),
            "\n".join(f"  [{i}] {u}" for i, u in enumerate(candidates)),
        )
        if not candidates:
            logger.info(
                "[image_search][candidates] query=%r 无可选 URL recent_urls 条数=%d",
                query,
                len(recent_urls),
            )
            return ("no_unique", [])

        results: list[tuple[str, str]] = []
        async with aiohttp.ClientSession(trust_env=True) as session:
            # 并行预取若干候选，缩短 n>1 时总耗时，降低「百炼已返回但下载阶段仍拖满宿主 RPC」的概率。
            prefetch_limit = min(len(candidates), max(count + 6, count * 2))
            prefetch_urls = [u for u in candidates[:prefetch_limit] if u]

            async def _fetch_pair(url: str) -> tuple[str, Optional[bytes]]:
                logger.info("[image_search][download_try] query=%r url=%s", query, url)
                data = await self._fetch_image(session, url)
                return url, data

            fetched: dict[str, Optional[bytes]] = {}
            if prefetch_urls:
                for url, data in await asyncio.gather(*[_fetch_pair(u) for u in prefetch_urls]):
                    fetched[url] = data

            for url in candidates:
                if len(results) >= count:
                    break
                if not url:
                    continue
                if url not in fetched:
                    logger.info("[image_search][download_try] query=%r url=%s", query, url)
                    fetched[url] = await self._fetch_image(session, url)
                image_data = fetched[url]
                if image_data:
                    b64 = base64.b64encode(image_data).decode("utf-8")
                    history.append((url, time.time()))
                    results.append((b64, url))
                    logger.info(
                        "[image_search][download_ok] query=%r engine=%s bytes=%d url=%s idx=%d/%d",
                        query,
                        ENGINE_LABEL,
                        len(image_data),
                        url,
                        len(results),
                        count,
                    )
                else:
                    logger.info("[image_search][download_fail] query=%r url=%s", query, url)

        if results:
            return ("ok", results)

        logger.warning(
            "[image_search][download_all_failed] query=%r 已尝试 %d 个 URL 均失败",
            query,
            len(candidates),
        )
        return ("all_failed", [])

    async def find_unique_image_b64(
        self,
        query: str,
    ) -> tuple[ImageSearchStatus, Optional[str], Optional[str]]:
        """兼容：等价于 ``find_images_b64(query, 1)`` 的首张结果。"""
        status, pairs = await self.find_images_b64(query, 1)
        if not pairs:
            return status, None, None
        b64, url = pairs[0]
        return ("ok", b64, url)

    @staticmethod
    async def _fetch_image(session: aiohttp.ClientSession, url: str) -> Optional[bytes]:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    return await response.read()
        except asyncio.TimeoutError:
            logger.warning("下载图片超时: %s", url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("下载图片失败 %s: %s", url, exc)
        return None
