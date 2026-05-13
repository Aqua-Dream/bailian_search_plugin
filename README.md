# 百炼联网搜索插件（`bailian_search`）

联网搜索与文搜图均依赖 **阿里云百炼（DashScope）** 的 [Responses API](https://help.aliyun.com/zh/model-studio/)：内置 `web_search`、`web_search_image` 工具，由百炼侧完成检索与整理。

当用户消息是 **可直接访问的 URL** 时，插件在本地抓取网页正文，再调用 **宿主 LLM**（`[models]` 中的 task）做摘要，与百炼搜索是两条独立路径。

## 功能概览

| 能力 | 实现方式 |
|------|----------|
| 联网问答 | 工具 `web_search` → 百炼 `web_search`，返回简体中文汇总 |
| 文搜图 / 发图 | 工具 `bailian_image_agent`（文搜图智能体；见下节「主 Agent / Planner」；需在 `[actions]` 中开启）→ 百炼 `web_search_image`，下载后发送 |
| URL 总结 | 检测到 http(s) 链接时本地抓取 + 宿主模型总结 |

## 主 Agent / Planner（重要）

自 **5.1.0** 起，文搜图对外工具名由 `image_search` 改为 **`bailian_image_agent`**。若 MaiSaka / Planner 的固定提示词或规则里仍写旧名，需改为新名并重载插件。

- **`web_search`**：仅用于**文字**联网检索。描述中已提示：**不要用本工具代替发图**；发图应使用 **`bailian_image_agent`**（百炼文搜图智能体）。误用文搜图会明显拉长单次工具耗时。
- **`bailian_image_agent`**：**一旦调用即会**请求百炼文搜图并发图（无额外布尔门闩）；请只在确实要发图时使用。
  - 参数 **`question`（字符串，推荐）**：**宜短**；优先用户原话，名称可能歧义时补最少作品名/领域（如「原神 千织 表情包」）。与 **`query`** 至少填其一。
  - 参数 **`query`（字符串，可选）**：兼容旧调用；简短检索短语。与 `question` 至少填其一。
  - 参数 **`n`（整数，可选，默认 1）**：要获取并发送的图片张数，**须为 1～5**。插件会**并行下载**多张候选，再**依次** `send.image`；张数越多总耗时越长，**非必要请保持 `n=1`**。
  - 当对话已经自然落在「想看图 / 讨图 / 要配图」时，传入 **`question`**（或 `query`）；若对方随口说要多张（如「来三张」），将 `n` 设为对应张数（不超过 5）。
- **`[actions].image_search_enabled = false`**：调用 `bailian_image_agent` 会立即失败（不访问百炼）。工具条目仍可能出现在 Planner 的工具列表中（插件无法在**不重载插件**的前提下从 schema 里动态「删除」该工具）；主 Agent 应**不要**再调用它。若你希望「关闭时列表里完全没有 `bailian_image_agent`」，需要宿主支持按配置过滤工具，或接受重载插件/重启进程。

## 文搜图行为说明

- **文搜图 `bailian_image_agent`** 走百炼 `web_search_image`；官方示例里单次常返回较多候选图。插件对 Responses **默认使用流式**：在 SSE 中解析出文搜图工具返回的图片列表后，**集齐前若干条（至少 5 条或不少于本次要发的张数 `n`）即结束 HTTP**，不再等待模型后续说明文字，以加快返回；若流式不可用则自动回退非流式。
- 文搜图与文字搜索已拆成两个工具，由 Planner 按需选用。

## 前置条件

1. 阿里云百炼 **API Key**（可写在 `[bailian].api_key`，或环境变量 `DASHSCOPE_API_KEY`）。
2. 所选 `responses_model` 须支持联网与文搜图（如官方文档中的 qwen-plus、qwen3.6-plus 等，以控制台为准）。

官方说明：

- [联网搜索 web_search](https://help.aliyun.com/zh/model-studio/web-search)
- [文搜图 web_search_image](https://help.aliyun.com/zh/model-studio/web-search-image)

## 依赖安装

在 **麦麦运行环境** 中进入本插件目录（例如 `plugins/xxxxx7258_google-search-plugin`），执行：

```bash
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
```

使用 uv 时：`uv pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple`

## 配置说明

配置文件：`plugins/xxxxx7258_google-search-plugin/config.toml`（路径随你的安装目录而定）。

### `[plugin]`

- `name`：配置内标识，默认 `bailian_search`。
- `version` / `config_version`：须与当前插件包一致，否则宿主可能拒绝加载。
- `enabled`：是否启用本插件。

### `[bailian]`（联网与文搜图）

- `api_key`：DashScope API Key；留空则读 `DASHSCOPE_API_KEY`。
- `base_url`：OpenAI 兼容网关，默认 `https://dashscope.aliyuncs.com/compatible-mode/v1`。
- `responses_model`：Responses 模型名，须支持 `web_search` 与 `web_search_image`。

文搜图在官方示例里常见一次返回较多张图；插件在发往百炼的提示词中会软性要求少张，但**无官方张数开关**。联网搜索侧会对拉取的群内上下文做 **约 6000 字截断**。

### `[models]`（仅 URL 直访总结）

- `model_name`：宿主侧 task，可选 `replyer`、`utils`、`planner`、`vlm`。仅用于 **用户输入为 URL** 时的页面摘要，**不**用于百炼联网正文。
- `temperature`、`context_time_gap`、`context_max_limit`：与宿主 LLM 调用相关。
- `llm_timeout_seconds`：单次宿主 LLM 调用上限（秒）。

### `[actions]`

- `image_search_enabled`：为 **true** 时才允许 `bailian_image_agent` 真正访问百炼；为 **false** 时调用立即失败。不影响 `web_search`。

**说明**：URL 直访抓取与文搜图候选条数等由插件内置固定策略（如单页抓取最长约 10 秒、正文上限约 3000 字、文搜图候选至多 15 条），不再提供 `[search_backend]` 等配置项。

## 使用与排障

- 需要时效信息、事实核对时，由 Planner 调用 **`web_search`**。
- 对话里适合发图且 `image_search_enabled=true` 时，由 Planner 调用 **`bailian_image_agent`**（调用即走文搜图并发图）。
- 在群内或私聊发送 **`/bailian_search_status`** 可查看当前版本、百炼 Key、文搜图开关与组件是否就绪。

## 鸣谢

- [MaiBot](https://github.com/MaiM-with-u/MaiBot)

---

## Star History

[![Star History Chart](https://api.star-history.com/image?repos=Aqua-Dream/bailian_search_plugin&type=timeline&legend=top-left)](https://www.star-history.com/?repos=Aqua-Dream%2Fbailian_search_plugin&type=timeline&legend=top-left)
