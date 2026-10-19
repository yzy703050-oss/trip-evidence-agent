---
name: query-info
description: 主Agent通过原生query工具查询出行事实并观察结果。
---

由主Agent直接调用 `query` 的query_plan，程序执行API并返回有界事实，无独立信息查询模型或正文JSON摘要控制器。纯查询不创建TripPlan；规划内部查询由复杂写服务展开，复用同一额度与调度。

按需要查询火车、酒店、攻略、天气或网页。外部参数遵守实际工具与供应商契约，未知条件不猜测。天气当前实况与指定日期预报分开；攻略链接保留真实标题和URL，链接本身不是已核实景点事实。

程序保存完整证据，先硬筛再按主Agent的ranking返回Top5；无ranking不暗加高铁或固定时段排序。缓存后页用 `read(resource=candidates)` 的offset，不把分页参数传供应商。刷新用query_plan item的refresh=true，历史结果不能冒充实时查询。

酒店地点缺少房价、空房和入住规则，不能当报价或预算满足；失败或空结果只说明本次查询范围，不能宣称客观无车无房。所有成功、空结果、部分和错误均回主Agent，由其统一答复。完整schema见 `trip_assistant/tools/registry.py`。
