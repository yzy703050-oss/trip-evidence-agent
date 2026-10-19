---
name: hotel-search
description: 查询经授权来源支持的hotel数据，保留来源和查询时间。
---

主Agent通过原生query工具的query_plan传入条件；参数以当前工具及供应商schema为准，不调用event_collection子Agent。
条件缺失或无效时返回 needs_input，不调用接口；未配置 Provider 时返回 unavailable。
Provider 成功结果原样保留来源、时间和空候选列表，异常返回 error。
禁止用模型、画像推断或历史示例补价格、库存和事实。
工具参数以当前 Provider schema 为准。高德地点搜索只必填 city，可选 keywords，默认“酒店”；无需为单纯搜地点追问入住日期和人数。
返回多个有来源的酒店地点，默认候选窗口 5 个。名称、地址、坐标、评分、电话和图片来自高德；房型、房价、空房始终未知。不得把 business.cost 当成每晚房价。
可选入住区间/人数只作用户条件记录；预算和空房硬条件未验证时明确说明，地点不能记入已知住宿费用。
原报价 Provider 仍必填 city（兼容 destination）、check_in、check_out、guests；离店须晚于入住，人数为正整数。

原始证据保存后，按硬条件及主Agent排序返回Top5，缓存后页通过read读取。所有结果回主Agent统一答复，不使用正文JSON控制执行。
