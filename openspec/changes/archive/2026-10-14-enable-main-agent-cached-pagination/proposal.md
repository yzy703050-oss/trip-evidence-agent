## Why

当前主流程在各领域都有候选、尚无校验问题时只允许提交草稿。主 Agent 因而不能根据首批候选是否适合整趟安排，自主读取缓存中的后续候选；有候选不代表已有可行组合。

## What Changes

- 允许主 Agent 在提交草稿前通过既有查询工具读取同一查询的后续本地缓存页；默认每页仍为 5 条。
- 模型判断是否需要下一页，程序验证当前任务、查询版本、参数、页偏移与缓存覆盖，不能以翻页触发供应商请求。
- 保留完整候选、准确的分页元数据及前页候选引用，翻页不刷新证据或增加供应商请求数。
- 保留已有停止、模型预算、硬条件、缺口披露及最终整趟校验；缓存耗尽不自动请求供应商下一页。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `consolidated-travel-execution`: 在先提交草稿的动作限制中开放受控的本地缓存翻页。
- `bounded-travel-tool-results`: 明确多页候选视图的准确偏移、缓存读取边界及引用保留。

## Impact

涉及 agents/workflow_runner.py、travel_prompts.py、context_projection.py、workflow_queries.py、travel_data/tools.py 和 batch_execution.py 及相关回归测试。不新增查询子模型或供应商接口，不改排序、每页条数或酒店报价能力。与未实施的 filter-candidates-by-hard-constraints 变更相互独立：该变更决定哪些候选进入分页，本变更决定主 Agent 能否继续读取分页。

## Assumptions

“第二次调用”理解为首次候选不合适时可继续读取后续缓存页，并非把次数硬编码为恰好两次；沿用既有决策上限。高德仍只抓取接口第 1 页、最多 25 条，接口翻页不在本次范围。用户于 2026-10-14 回复“开始修改”，确认此前提出的本地翻页范围。
