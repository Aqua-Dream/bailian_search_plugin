"""网页正文抓取（trafilatura / readability / BeautifulSoup 降级）。"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup
from readability import Document

logger = logging.getLogger(__name__)

# URL 直访抓取：固定策略（不再暴露为配置项）
_FETCH_CONTENT_TIMEOUT = 10
_FETCH_MAX_CONTENT_LENGTH = 3000
_DEFAULT_USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]


class ContentFetcher:
    """单页正文抓取（供 URL 直访总结使用）。"""

    async def fetch_single(self, session: aiohttp.ClientSession, url: str) -> Optional[str]:
        """抓取单个页面的正文内容。"""
        timeout = _FETCH_CONTENT_TIMEOUT
        max_length = _FETCH_MAX_CONTENT_LENGTH

        try:
            user_agents = list(_DEFAULT_USER_AGENTS) or [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ]
            headers = {"User-Agent": random.choice(user_agents)}

            request_kwargs: dict = {"timeout": aiohttp.ClientTimeout(total=timeout), "headers": headers}

            async with session.get(url, **request_kwargs) as response:
                if response.status != 200:
                    logger.warning("抓取失败 %s 状态码 %s", url, response.status)
                    return None

                html_bytes = await response.read()
                try:
                    html = html_bytes.decode(response.charset or "utf-8")
                except (UnicodeDecodeError, TypeError):
                    try:
                        html = html_bytes.decode("gbk", errors="ignore")
                    except UnicodeDecodeError:
                        html = html_bytes.decode("utf-8", errors="ignore")

                try:
                    import trafilatura

                    extracted = trafilatura.extract(
                        html,
                        include_comments=False,
                        include_tables=True,
                        no_fallback=False,
                    )
                    if extracted and len(extracted.strip()) > 100:
                        logger.debug("trafilatura 提取成功 %s", url)
                        return extracted.strip()[:max_length]
                except ImportError:
                    logger.debug("trafilatura 未安装,跳过")
                except Exception as exc:  # noqa: BLE001
                    logger.debug("trafilatura 提取失败: %s", exc)

                try:
                    doc = Document(html, min_text_length=50, retry_length=250, url=url)
                    summary_html = doc.summary()
                    soup = BeautifulSoup(summary_html, "lxml")
                    readability_text = soup.get_text(separator="\n", strip=True)
                    if readability_text and len(readability_text) > 100:
                        logger.debug("readability 提取成功 %s", url)
                        return readability_text[:max_length]
                except Exception as exc:  # noqa: BLE001
                    logger.debug("readability 提取失败: %s", exc)

                try:
                    soup = BeautifulSoup(html, "lxml")
                    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                        tag.decompose()
                    fallback = soup.get_text(separator="\n", strip=True)
                    logger.debug("BeautifulSoup 兜底 %s", url)
                    return fallback[:max_length] if fallback else None
                except Exception as exc:  # noqa: BLE001
                    logger.error("BeautifulSoup 兜底也失败: %s", exc)
                    return None

        except asyncio.TimeoutError:
            logger.warning("抓取超时: %s", url)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error("抓取未知错误 %s: %s", url, exc)
            return None
