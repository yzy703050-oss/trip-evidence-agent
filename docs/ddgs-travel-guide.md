# DDGS 旅游攻略链接（V0）

CLI 启动时为信息获取 Agent 的 `travel_guide` 工具注入 `DDGSGuideProvider`。
例如“给我北京旅游攻略链接”：只需目的地，不需要日期或人数，也不需要搜索 API Key 或 MCP 服务。

搜索关键词为“目的地 旅游攻略”，检索最多五条，优先返回标题或摘要包含“攻略”的有效 HTTP(S) 链接，否则返回第一个有效链接。
通过 DDGS 的 Bing 搜索源检索，避免自动选择只返回百科结果的搜索源。
不限定小红书或其他平台，不读取网页正文，不生成行程或景点事实。
标题和原始 URL 放在 `GuideFact.content` 中，`kind=link`，`source.url` 保留全部查询参数。
`verification=verified` 仅表示链接来自真实检索，结果说明明确注明网页内容未核验。
结果沿用现有来源校验、最终答复与 CLI 展示链路。

检索在线程中执行，默认十秒超时；无结果和查询失败均返回明确说明，不编造替代链接。
DDGS 可受搜索引擎限流或网络影响，不能保证每次查询都有结果。

安装现有 `requirements.txt`；固定 `primp==0.15.0`，避免 DDGS 9.10.0 与 primp 2.x 的浏览器模拟参数冲突。
离线验证：`python -m pytest -q tests/test_ddgs_guide.py`。
