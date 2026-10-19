---
name: travel-guide
description: 查询经授权来源支持的guide数据，保留来源和查询时间。
---

主Agent通过原生query工具的query_plan传入条件；参数以当前工具及供应商schema为准，不调用event_collection子Agent。
条件缺失或无效时返回 needs_input，不调用接口；未配置 Provider 时返回 unavailable。
Provider 成功结果原样保留来源、时间和空候选列表，异常返回 error。
禁止用模型、画像推断或历史示例补价格、库存和事实。
必填 destination；visit_dates 可为空，日期缺失时 Provider 只可返回不依赖日期的地点候选，不声称当天状态。

原始证据保存后，按硬条件及主Agent排序返回Top5，缓存后页通过read读取。所有结果回主Agent统一答复，不使用正文JSON控制执行。
