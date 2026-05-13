"""百炼联网搜索插件（bailian_search）主入口

联网搜索与文搜图均使用阿里云百炼 Responses API（OpenAI 兼容模式）。
URL 直访仍为本地抓取 + 宿主 LLM 总结。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from maibot_sdk import Command, MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .config import BailianSearchPluginConfig
from .pipelines.bailian_responses import BailianResponsesClient, resolve_dashscope_api_key
from .pipelines.content_fetcher import ContentFetcher
from .pipelines.image_search_pipeline import ImageSearchPipeline
from .pipelines.llm_runner import LLMRunner
from .pipelines.search_pipeline import SearchPipeline
from .pipelines.url_pipeline import UrlPipeline, is_url

# 百炼 Responses HTTP 超时（秒）。宿主对 plugin.invoke_tool 等 RPC 超时默认 60s，
# 此处须更小并留余量，否则会出现「宿主已超时、插件仍在等百炼 HTTP」。
_BAILIAN_RESPONSES_TIMEOUT_SECONDS: float = 55.0


def _strip_tool_string(value: Any) -> str:
    """将工具入参转为去首尾空白的字符串；``None`` 与非字符串会安全转为字符串再 strip。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _resolve_image_search_intent_text(
    question: Any,
    query: Any,
    kwargs: dict[str, Any],
) -> str:
    """解析文搜图意图文本：优先 ``question``（自然语言），其次 ``query``（兼容旧调用）。"""
    for v in (
        question,
        query,
        kwargs.get("question"),
        kwargs.get("query"),
    ):
        s = _strip_tool_string(v)
        if s:
            return s
    return ""


def _parse_image_search_count_n(value: Any) -> tuple[bool, int, str]:
    """解析文搜图张数 ``n``：合法范围为 1～5。返回 ``(是否合法, n, 错误说明)``。"""
    if value is None or value == "":
        return True, 1, ""
    if isinstance(value, bool):
        return False, 0, "参数 n 须为 1～5 的整数，不能为布尔值"
    try:
        n = int(value)
    except (TypeError, ValueError):
        return False, 0, "参数 n 须为 1～5 的整数"
    if not (1 <= n <= 5):
        return False, 0, "参数 n 须在 1～5 之间（表示要获取并发送的图片张数）"
    return True, n, ""


class BailianSearchPlugin(MaiBotPlugin):
    """百炼联网搜索插件主类"""

    config_model = BailianSearchPluginConfig

    _bailian: Optional[BailianResponsesClient]
    _content_fetcher: Optional[ContentFetcher]
    _llm_runner: Optional[LLMRunner]
    _search_pipeline: Optional[SearchPipeline]
    _url_pipeline: Optional[UrlPipeline]
    _image_pipeline: Optional[ImageSearchPipeline]

    def __init__(self) -> None:
        super().__init__()
        self._bailian = None
        self._content_fetcher = None
        self._llm_runner = None
        self._search_pipeline = None
        self._url_pipeline = None
        self._image_pipeline = None

    async def on_load(self) -> None:
        self._build_pipelines()
        cfg = self.config
        has_key = bool(resolve_dashscope_api_key(cfg.bailian.api_key))
        self.ctx.logger.info(
            "bailian_search v%s 已加载 (百炼模型=%s, api_key=%s, image_search=%s)",
            cfg.plugin.version,
            cfg.bailian.responses_model,
            "已配置" if has_key else "未配置",
            cfg.actions.image_search_enabled,
        )

    async def on_unload(self) -> None:
        self.ctx.logger.info("bailian_search 已卸载")

    async def on_config_update(
        self,
        scope: str,
        config_data: dict[str, Any],
        version: str,
    ) -> None:
        del config_data
        self.ctx.logger.info("配置更新事件: scope=%s version=%s,重建 pipelines", scope, version)
        try:
            self._build_pipelines()
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("重建 pipelines 失败: %s", exc, exc_info=True)

    def _build_pipelines(self) -> None:
        cfg = self.config
        key = resolve_dashscope_api_key(cfg.bailian.api_key)
        if key:
            self._bailian = BailianResponsesClient(
                api_key=key,
                base_url=cfg.bailian.base_url.strip(),
                model=cfg.bailian.responses_model.strip(),
                timeout_seconds=_BAILIAN_RESPONSES_TIMEOUT_SECONDS,
            )
        else:
            self._bailian = None
            self.ctx.logger.warning(
                "百炼 API Key 未配置：联网搜索与文搜图不可用；请填写 [bailian].api_key 或环境变量 DASHSCOPE_API_KEY",
            )

        self._content_fetcher = ContentFetcher()
        self._llm_runner = LLMRunner(self.ctx, cfg.models)
        self._search_pipeline = SearchPipeline(
            self.ctx,
            models_cfg=cfg.models,
            bailian=self._bailian,
        )
        self._url_pipeline = UrlPipeline(
            content_fetcher=self._content_fetcher,
            llm_runner=self._llm_runner,
        )
        self._image_pipeline = ImageSearchPipeline(bailian=self._bailian)

    async def _resolve_bot_name(self) -> str:
        try:
            value = await self.ctx.config.get("bot.nickname", "")
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug("config.get bot.nickname 失败: %s", exc)
            value = ""
        return str(value).strip() or "机器人"

    def _ensure_pipelines_ready(self) -> bool:
        required = (
            self._content_fetcher,
            self._llm_runner,
            self._search_pipeline,
            self._url_pipeline,
            self._image_pipeline,
        )
        if all(v is not None for v in required):
            return True
        self.ctx.logger.warning("pipelines 未就绪,尝试重建")
        try:
            self._build_pipelines()
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("pipelines 重建失败: %s", exc, exc_info=True)
            return False
        return True

    @Tool(
        "web_search",
        description=(
            "联网搜索工具（阿里云百炼 web_search）。当用户有疑问、需要时效信息或不确定的事实时调用，"
            "用简体中文汇总检索结果。"
            "``question`` 宜为**简短、可直接用于检索**的表述；若名称可能歧义，可从聊天上下文补最少量的作品名或领域。"
            "**注意**：本工具只做文字联网检索。**发图**请改用独立工具 **`bailian_image_agent`**（百炼文搜图智能体），勿用本工具代替。"
        ),
        parameters=[
            ToolParameterInfo(
                name="question",
                param_type=ToolParamType.STRING,
                description="检索主题或问题：宜短、宜直；必要时据语境补作品名等以消歧。",
                required=True,
            ),
        ],
    )
    async def handle_web_search(
        self,
        question: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> dict[str, str]:
        del kwargs

        question = (question or "").strip()
        if not question:
            return {"name": "web_search", "content": "问题为空，无法执行搜索。"}

        if not self._ensure_pipelines_ready():
            return {"name": "web_search", "content": ""}

        bot_name = await self._resolve_bot_name()

        try:
            if is_url(question):
                self.ctx.logger.info("检测到 URL 输入,直接访问并总结: %s", question)
                content = await self._url_pipeline.run(  # type: ignore[union-attr]
                    question,
                    bot_name=bot_name,
                )
            else:
                self.ctx.logger.info("百炼联网搜索,原始问题: %s", question)
                content = await self._search_pipeline.run(  # type: ignore[union-attr]
                    question,
                    chat_id=stream_id,
                    bot_name=bot_name,
                )
            return {"name": "web_search", "content": content}
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("web_search 执行异常: %s", exc, exc_info=True)
            return {"name": "web_search", "content": ""}

    @Tool(
        "bailian_image_agent",
        description=(
            "百炼「文搜图」智能体（百炼 `web_search_image`）：联网按你的描述找图、筛选并**直接发图**给用户，"
            "不是单纯返回一段文字说明。**被调用即会**走完整文搜图链路，无额外确认参数。"
            "适合对话已自然聊到「想看 / 要找 / 发一张和某话题相关的图」——例如角色立绘、壁纸、梗图、配图等。"
            "``question`` / ``query`` 宜为**简短检索句**：优先用户原话；名称可能歧义时，用最少字数补作品或领域（如「原神 千织 表情包」）。"
            "文搜图链路偏慢，宿主侧单次工具 RPC 默认约 60 秒量级上限；普通闲聊或纯文字问答请不要选用本工具。"
        ),
        parameters=[
            ToolParameterInfo(
                name="question",
                param_type=ToolParamType.STRING,
                description="搜图检索句：宜短；优先用户原话，必要时仅补作品名或游戏名等以消歧。与 ``query`` 至少填其一。",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="兼容旧版：简短图片检索短语。与 ``question`` 至少填其一。",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="n",
                param_type=ToolParamType.INTEGER,
                description=(
                    "要获取并发送的图片张数，须为 1～5。"
                    "宿主对单次工具 RPC 默认约 60s：百炼文搜图本身仍偏慢，`n>1` 时仍可能逼近上限。"
                    "超时后 Planner 常以为失败而重试或多轮 reply，用户侧可能看到重复话术；非必要请用 `n=1`。"
                    "多图时插件会并行下载候选、再依次发图。"
                ),
                required=False,
                default=1,
            ),
        ],
        associated_types=["image"],
        parallel_action=False,
    )
    async def handle_image_search(
        self,
        question: str = "",
        query: str = "",
        n: Any = 1,
        stream_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str]:
        intent_text = _resolve_image_search_intent_text(question, query, kwargs)
        del kwargs

        if not self.config.actions.image_search_enabled:
            if stream_id:
                await self.ctx.send.text(
                    "图片搜索功能当前未启用。如需使用，请在配置文件中启用此功能。",
                    stream_id,
                )
            return False, "图片搜索功能未启用"

        if not intent_text:
            if stream_id:
                await self.ctx.send.text("你想搜什么图片呀？", stream_id)
            return False, "搜图内容为空：请传入 question（自然语言，推荐）或 query（兼容）"

        ok_n, want_n, n_err = _parse_image_search_count_n(n)
        if not ok_n:
            return False, n_err

        if not self._ensure_pipelines_ready():
            return False, "图片搜索组件未就绪"

        try:
            self.ctx.logger.info("开始文搜图: %s want_n=%d", intent_text, want_n)
            status, pairs = await self._image_pipeline.find_images_b64(  # type: ignore[union-attr]
                intent_text,
                want_n,
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("图片搜索动作异常: %s", exc, exc_info=True)
            if stream_id:
                await self.ctx.send.text(f"搜索图片时出错了：{exc}", stream_id)
            return False, f"图片搜索失败: {exc}"

        if status == "ok":
            sent = 0
            try:
                for b64, url in pairs:
                    await self.ctx.send.image(b64, stream_id)
                    sent += 1
                    self.ctx.logger.info("成功发送图片 %d/%d url=%s", sent, len(pairs), url)
                msg = f"已发送 {len(pairs)} 张图片"
                if len(pairs) < want_n:
                    msg += f"（请求 {want_n} 张，其余候选不足或下载失败）"
                return True, msg
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.error("send.image 失败: %s", exc, exc_info=True)
                if stream_id:
                    await self.ctx.send.text("我下载好了图片，但是发送失败了...", stream_id)
                detail = f"已发送 {sent} 张后失败" if sent else "发送图片失败"
                return False, f"{detail}: {exc}"

        # 不在此处 send.text：MaiSaka Planner 可能在多轮中多次调用本工具并更换关键词，
        # 每条失败都发消息会导致用户连续收到多条「没找到图片」。结论交给 Planner 汇总为一条回复即可。
        if status == "no_results":
            return False, f"未找到与「{intent_text}」相关的图片（文搜图无可用结果）"

        if status == "no_unique":
            if stream_id:
                await self.ctx.send.text(
                    "最近30分钟内已经发过相关图片了，先休息一下吧。",
                    stream_id,
                )
            return False, "30 分钟内图片重复"

        if stream_id:
            await self.ctx.send.text("找到了图片，但下载都失败了，可能是网络问题。", stream_id)
        return False, "所有图片下载失败"

    @Command(
        "bailian_search_status",
        description="查询百炼联网搜索插件当前加载状态与关键配置",
        pattern=r"^/bailian_search_status\s*$",
    )
    async def handle_status(
        self,
        stream_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        del kwargs

        cfg = self.config
        has_key = bool(resolve_dashscope_api_key(cfg.bailian.api_key))

        ready = all(
            v is not None
            for v in (
                self._content_fetcher,
                self._llm_runner,
                self._search_pipeline,
                self._url_pipeline,
                self._image_pipeline,
            )
        )

        lines = [
            f"百炼联网搜索插件 (bailian_search) v{cfg.plugin.version}",
            f"百炼模型: {cfg.bailian.responses_model}",
            f"百炼 API Key: {'已配置' if has_key else '未配置'}",
            f"宿主 LLM task: {cfg.models.model_name}（URL 总结）",
            f"图片搜索: {'已启用' if cfg.actions.image_search_enabled else '未启用'}",
            f"组件就绪: {'是' if ready else '否'}",
        ]
        message = "\n".join(lines)

        if stream_id:
            await self.ctx.send.text(message, stream_id)
        return True, message, True


def create_plugin() -> BailianSearchPlugin:
    return BailianSearchPlugin()


_logger = logging.getLogger(__name__)
