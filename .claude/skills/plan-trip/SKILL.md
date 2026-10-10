---
name: plan-trip
description: 主 Agent 根据结构化条件和有来源的 domain_results 生成行程的专属指南。
---

MainAgent 负责行程模型调用。本指南仅在主 Agent 行程阶段加载，harness 先按需执行
偏好、记忆、RAG 和信息获取任务。读取 original_query、effective_preferences、
travel_conditions 和 domain_results。缺用户条件返回 needs_input；跨领域冲突可返回
一次定向 needs_requery，给出 reason、domains 和可执行 constraints，不改变用户硬条件。
独立脚本复用 cli.TripEvidenceCLI 的统一入口，本能力不作为子 Agent 注册。

只生成安排建议：每日日期、时间、活动地点引用。城市只使用 city_ref="guide_destination"（展示查询中的目的地）；活动只使用 location_ref="guide:<攻略事实下标>"，引用有来源的 place/poi/attraction/location 类型事实。没有查到具体地点时，只能使用固定枚举 suggestion:city_walk、suggestion:museum、suggestion:meal、suggestion:rest，展示地点待核实的通用活动框架。不得输出自由文本 city/location 或自行构造展示名。火车和酒店选择必须引用 domain_results 中的报价 ID（selected_train_id、selected_hotel_id）；不存在可用候选项时返回 null。
价格、车次、房型、库存、取消规则、天气、开放时间与预约规则均由程序从对应查询结果重建。不得根据季节、常识或网页摘要补齐这些事实，不生成 estimated_budget 或预算内保证。
输出 JSON：{"itinerary":{"daily_plans":[{"day":1,"date":"2026-10-08","activities":[{"time":"09:00-12:00","location_ref":"suggestion:city_walk"}]}]},"selected_train_id":null,"selected_hotel_id":null,"planning_complete":true}。
活动时间仅是安排建议；实际开放与预约仍须查看有来源的攻略事实，未知信息保持待核实。

## 多目的地交通住宿工作流

读取用户原文、完整任务概览、当前任务条件、条件来源、候选查询视图、前段边界和执行反馈。
每段只安排火车和住宿，不生成 activities、景点、天气或市内通勤时间。
候选选择引用 query_id/result_revision/candidate_id；返程酒店引用为 null，详情由程序重建。
一段草稿不等于全程成功，后段可能要求重检前段；所有当前版本草稿经 validate_workflow 才能完成。
抵达日期只采用可靠证据，未知保持 null；拟入住/离店日期与用户固定日期、推导、提案分开。
后段边界可靠且日期可灵活安排时可以提出建议，否则询问用户批准，不修改固定条件。
重大冲突输出原因、受影响任务和具体修改建议，ask_user 保存断点；用户回复后从修改点恢复。
酒店地点只有地址和位置等参考事实，价格、库存与入住规则未知；有硬报价或预算要求时不能 completed。
全程费用按每段人数和报价合计，未知不能算零。无新资料或达到上限时 partial，保留已知安排与缺口。
