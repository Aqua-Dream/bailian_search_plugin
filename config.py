"""百炼联网搜索插件（bailian_search）配置模型。

联网与文搜图统一走阿里云百炼 Responses API（OpenAI 兼容模式）。
"""

from typing import Literal

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    """插件基础信息"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    name: str = Field(default="bailian_search", description="插件名称（配置标识）")
    version: str = Field(default="5.1.2", description="插件版本")
    config_version: str = Field(default="5.1.2", description="配置版本(Runner 用于兼容性校验)")
    enabled: bool = Field(default=True, description="是否启用插件")


class ModelsSection(PluginConfigBase):
    """URL 直访总结等场景使用的宿主 LLM task 参数。"""

    __ui_label__ = "模型"
    __ui_icon__ = "brain"
    __ui_order__ = 1

    model_name: Literal["replyer", "utils", "planner", "vlm"] = Field(
        default="replyer",
        description=(
            "宿主模型 task（replyer/utils/planner/vlm），用于 URL 抓取后的本地总结。"
            "联网搜索正文由百炼 Responses API 直接生成。"
        ),
    )
    temperature: float = Field(default=0.7, description="模型生成温度")
    context_time_gap: int = Field(default=300, description="拉取最近多少秒的全局聊天作为上下文")
    context_max_limit: int = Field(default=15, description="最多拉取多少条全局聊天作为上下文")
    llm_timeout_seconds: int = Field(
        default=60,
        description="单次宿主 LLM 调用超时(秒)",
    )


class ActionsSection(PluginConfigBase):
    """动作组件开关"""

    __ui_label__ = "动作"
    __ui_icon__ = "zap"
    __ui_order__ = 2

    image_search_enabled: bool = Field(
        default=False,
        description=(
            "是否允许执行文搜图工具 bailian_image_agent（百炼 web_search_image）。"
            "关闭时调用 bailian_image_agent 会立即失败；仍建议主 Agent 不要调用以减小提示噪声。"
        ),
    )


class BailianSection(PluginConfigBase):
    """阿里云百炼（DashScope）OpenAI 兼容 Responses API"""

    __ui_label__ = "百炼 API"
    __ui_icon__ = "cloud"
    __ui_order__ = 3

    api_key: str = Field(
        default="",
        description="API Key；留空则使用环境变量 DASHSCOPE_API_KEY",
    )
    base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        description="OpenAI 兼容网关 Base URL",
    )
    responses_model: str = Field(
        default="qwen3.6-plus",
        description=(
            "Responses API 模型名，须支持 web_search 与 web_search_image。"
            "可参考文档：qwen-plus、qwen3-max、qwen3.6-plus 等。"
        ),
    )


class BailianSearchPluginConfig(PluginConfigBase):
    """百炼联网搜索插件顶层配置"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    models: ModelsSection = Field(default_factory=ModelsSection)
    actions: ActionsSection = Field(default_factory=ActionsSection)
    bailian: BailianSection = Field(default_factory=BailianSection)
