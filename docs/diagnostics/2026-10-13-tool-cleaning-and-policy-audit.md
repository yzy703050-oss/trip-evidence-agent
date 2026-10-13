# 政策与工具数据清洗检查

2026-10-13；只检查现状并更新待开发方案，未修改产品或测试代码，未请求真实供应商、模型或企业RAG索引。正式需求见[OpenSpec](../../openspec/changes/consolidate-travel-model-calls/proposal.md)，实施任务仍待完成。

## 当前已有能力

- travel_data/juhe_train.py:26–112：接口字段提取、时间/价格/库存校验、异常项排除、TrainOffer归一化。
- travel_data/amap_hotel.py:26–95与hotel_places.py：字段提取、住宿地点类型校验及地点id去重；真实接口仅酒店地点，没有房型报价和空房。
- travel_data/public_query.py:39–61：weather提取指定日期或当前温度，不返回完整天气HTTP响应。
- travel_data/candidates.py:43–95：火车/酒店按支持的硬约束过滤、偏好排序和候选分页；非火车/酒店直接返回。
- travel_data/tools.py:237与agents/context_projection.py:152–178：领域校验及主视图白名单，默认5候选；缓存/审计与模型输入分离。

## 已确认缺口

1. current weather字段为temp_c，但FACT_FIELDS没有temp_c；主投影删除query，天气城市不能单靠领域结果保留。合成上海当前22℃输入，provider有temp_c，投影items只剩date/description/source。
2. 7条重复日期的合成预报在工具candidate_view仍有7条；主投影有5条，且description仍各2000字符。缺少工具领域去重及自由文本长度限制。
3. hotel_place_view保留unverified_constraints，但主投影DOMAIN_FIELDS没有该字段；不能只留下partial而丢掉哪些预算/库存条件未核实。
4. source投影目前只保留provider，不保留结果fetched_at；后续紧凑视图需用结果层来源索引保留身份和获取时间，不重复完整URL。
5. .claude/skills/ask-question/script/agent.py:333–347的service分支拼接answer与citations全文返回；本地分支生成通用回答。两者都不是新要求的政策理解/带来源JSON约束提取契约。后续复用检索后直接理解提取，避免通用答复后再转JSON。

## 检查证据

命令：`.\.venv\Scripts\python.exe -m pytest tests/test_juhe_train_provider.py tests/test_amap_hotel.py tests/test_information_tools.py tests/test_compact_context.py -q`

结果：80 passed，1条已有dashscope弃用警告，退出码0。pytest退出清理阶段另报系统临时目录pytest-current的PermissionError；未影响测试断言结果，未为消除该提示删除目录。

两项只读合成检查直接调用PublicQueryProvider的注入weather_fetch、candidate_view和project_domain_result，无网络/模型调用；输出见上述缺口1/2。现有测试通过说明已有路径的覆盖通过，不能证明新清洗要求实现；新增回归测试和修补列入[详细计划](../superpowers/plans/2026-10-12-consolidate-travel-model-calls.md)Task 3/5（工具任务因新增立即状态简化任务而顺延）。
