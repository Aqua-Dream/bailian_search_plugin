"""百炼联网搜索插件（bailian_search）业务流水线模块。

- bailian_responses: 百炼 Responses API（web_search / web_search_image）
- prompts:           百炼输入模板与 URL 总结模板
- llm_runner:        宿主 LLM 调用包装（URL 总结）
- content_fetcher:   网页正文抓取
- url_pipeline:      URL 直访总结
- search_pipeline:   联网搜索（百炼）
- image_search_pipeline: 文搜图（百炼）+ 下载去重
"""
