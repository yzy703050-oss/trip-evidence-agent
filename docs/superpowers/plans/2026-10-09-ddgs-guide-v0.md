# V0 DDGS 攻略链接实施计划

用户已确认用 DDGS 搜索，不限定小红书，返回一个结果，并要求合并到 main。
本次目标为 trip-evidence-agent 独立 V0 仓库；外层 V2 不参与合并。
本仓库未初始化 OpenSpec，因此沿用现有工具契约，不创建 OpenSpec 目录。
单代理实施、整体自审；保留当前其他开发任务的未提交内容。

## 设计与验收

- 为已有 travel_guide 工具提供 DDGSGuideProvider，并在 CLI 启动时注入。
- 输入沿用 GuideQuery：destination 必填，visit_dates 可选，不追问无关条件。
- 搜索“目的地 旅游攻略”，最多检索五条，优先选择标题或摘要包含“攻略”的有效 HTTP(S) 链接，否则取首个有效链接。
- 沿用 GuideFact，kind=link，content 含标题和原始 URL，来源记录 DDGS 与抓取时间。
- verified 仅表示链接来自实际搜索结果，不表示网页攻略内容已核验；不提取景点事实。
- 空结果说明未找到；失败/超时说明查询失败，不能伪造链接。
- 调用 DDGS 而非 MCP；保留链接查询参数，现有最终答复和 CLI 能展示链接。

## 实施步骤

1. 在 tests/test_ddgs_guide.py 先写供应商、失败/空结果、时间限制、CLI 注入与工具到答复链路测试，确认因缺少实现失败。
2. 新增 travel_data/ddgs_guide.py，以异步线程包装同步 DDGS；注入检索函数便于离线验证。
3. 在 cli.py 注册供应商，在 query-info 技能提示中说明攻略返回单个链接；保持其他工具行为。
4. requirements.txt 固定与 ddgs 9.10.0 兼容的 primp 0.15.0，记录使用方式。
5. 运行新增测试、全量 pytest、差异检查，尝试一次真实 DDGS 查询；核对测试与设计一致。
6. 提交本分支、快进合并到 V0 main，在 main 上复测；不合入酒店任务的未提交内容。

验证命令：项目运行环境 `python -m pytest -q`、`git diff --check`。
基线：250 passed，3 skipped。完成条件：单链接及异常场景通过，CLI 可显示原始 URL，main 包含已验证提交。

## 实施记录

- 新增九项场景测试先因缺少 DDGSGuideProvider 失败，最小实现后与启动接线测试共十五项通过。
- 真实搜索发现旅游大学排在攻略前；新增相关性回归测试确认失败，再增加本地优先选择规则，不限制网站。
- DDGS auto 搜索源可能只返回百科页面；验证 Bing 返回真实攻略候选，增加回归断言并采用 Bing 搜索源。
- 最终全量测试：260 passed，3 skipped；git diff --check 通过。
- 真实查询“北京”返回一条知乎攻略链接：https://zhuanlan.zhihu.com/p/638327554 。
- 整体自审：沿用现有 GuideFact、来源校验、信息工具缓存和答复展示；不引入额外模型或 MCP，不改酒店/火车契约。
- 规格核对：目的地必填、日期可选、一个原始链接、平台不限、失败和空结果不编造，均有实现与测试证据。仓库未启用 OpenSpec verify/archive，未声称运行这些工作流。
