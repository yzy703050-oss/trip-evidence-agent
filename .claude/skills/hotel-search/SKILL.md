---
name: hotel-search
description: 查询经授权来源支持的hotel数据，保留来源和查询时间。
---

读取 context 与 previous_results 中 event_collection 的已确认结构化条件。
条件缺失或无效时返回 needs_input，不调用接口；未配置 Provider 时返回 unavailable。
Provider 成功结果原样保留来源、时间和空候选列表，异常返回 error。
禁止用模型、画像推断或历史示例补价格、库存和事实。
必填 city（兼容 destination）、check_in、check_out、guests；离店须晚于入住，人数为正整数。
