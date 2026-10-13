# 真实模型与 RAG 实测记录

2026-10-13，模型 deepseek-flash，thinking disabled，max_tokens=1116。通过生产 MainAgent / ExecutionHarness / WorkflowRunner / SDK 执行，记录供应商实际 usage，不使用离线 token 估算。每个场景只执行一次，没有挑选成功样本。

**结论：3 个行程均失败，8 次模型请求成功返回，共 13,451 token。** 失败发生在结构化输出校验，三轮都没有执行火车/酒店供应商请求。因此尚未验证超标酒店局部重查是否成功，也不能拿本次失败耗时当作成功行程性能。

## 场景结果

| 场景 | 输入 token | 输出 token | 总 token | 请求耗时 | 模型调用 | 行程结果 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| tourism_live | 2183 | 722 | 2905 | 3.722s | 2 | error / 未成功 |
| business_live | 3421 | 1905 | 5326 | 8.031s | 3 | error / 未成功 |
| business_conflict | 3439 | 1781 | 5220 | 10.886s | 3 | error / 未成功 |

## 逐次模型调用

| 场景 | 阶段 | 输入 | 输出 | 调用延时 | 首 token 延时 |
| --- | --- | ---: | ---: | ---: | ---: |
| tourism_live | main:plan | 1359 | 461 | 2.121s | 0.455s |
| tourism_live | main:repair_proposal | 824 | 261 | 1.549s | 0.324s |
| business_live | agent:travel_policy | 988 | 874 | 3.530s | 0.243s |
| business_live | main:plan | 1464 | 543 | 2.563s | 0.536s |
| business_live | main:repair_proposal | 969 | 488 | 1.819s | 0.406s |
| business_conflict | agent:travel_policy | 988 | 670 | 3.185s | 0.635s |
| business_conflict | main:plan | 1464 | 558 | 4.989s | 2.935s |
| business_conflict | main:repair_proposal | 987 | 553 | 2.586s | 1.121s |

## 实际 RAG 与虚构制度

本地 BAAI/bge-small-zh-v1.5 + 项目 RAGKnowledgeAgent + Milvus Lite，新建隔离临时数据库并导入 3 段文档，未写入现有生产知识库。冷启动与入库耗时 11.271s，未计入表中单次请求延时；两轮实际向量检索耗时分别 62.876ms、59.159ms，均检索到 3 段来源。本地 embedding 不调用收费 API，不计入 LLM token。

- [当前制度](policy-1.txt)：二等座，火车单程 700 元内，上海住宿含税 500 元/晚。
- [废止制度](policy-2.txt)：旧 900 元标准已被当前制度取代。
- [例外审批](policy-3.txt)：800 元例外须有有效审批编号，本用户没有审批。

计划冲突：先返回 700/760 元每晚的模拟酒店，刷新后返回 380/420 元，预期只重查酒店、复用两段火车。本轮没有进入供应商阶段，模拟报价未被使用。真实酒店接口为高德地点接口，不提供报价/库存，不可将地点算作政策合规报价。

## 校验失败原因（原输出离线重放，无额外模型费用）

- tourism_live / main:plan：`unsupported workflow conditions`。
- tourism_live / main:repair_proposal：`invalid task purpose`。
- business_live / agent:travel_policy：`invalid applicability`。
- business_live / main:plan：`unsupported workflow conditions`。
- business_live / main:repair_proposal：`invalid hotel keywords`。
- business_conflict / agent:travel_policy：`invalid applicability`。
- business_conflict / main:plan：`unsupported workflow conditions`。
- business_conflict / main:repair_proposal：`invalid hotel keywords`。

证据定位：Main.initialize 的提示未给出完整 conditions/constraints 类型清单，首次输出把 purpose、seat_class 等放入 confirmed_conditions；修正输出又用了不支持的 task.purpose=tourism，或 hotel_keywords 数组。旅游修正还把单程 700 元错误改为全程 total_budget_cny=700，不能通过放松校验绕过。

政策提取提示只列了字段名称，未明确 applicability 必须是对象；两轮都输出字符串，validate_policy_context 拒绝为 invalid applicability，Runner 将其变为 policy_evidence 缺口。第二轮还输出 seat_class.value=null。政策输出也把尚未查到的酒店/火车报价写入政策 missing_fields，混淆了制度资料缺失和后续供应商数据缺失。

建议后续修复源头提示与结构契约，并用本次原始输出补回归；不要删除强制字段、放宽校验或改写用户预算。本次仅测量、保存证据和定位，没有修改产品逻辑，也没有反复调用模型尝试刷出成功结果。

## 计量边界与复现

token 为供应商 usage 的 input + output。费用没有计算；DeepSeek 缓存命中/未命中计费明细不在当前项目适配的 usage 中。这些数字只代表本次三轮，不能据此确认之前那次是否花了 5 万 token。模型/API 返回成功与业务任务成功分别记录。每轮模型上限 6 次、调度决定 4 次、请求 240 秒（外层 260 秒），仅评测配置，生产配置未改变。

重新运行真实测试（会再次产生 API 用量）：
`python evals/run_live_policy_measurement.py --output docs/evals/新的目录`。
只分析已有记录（零 API）：
`python evals/analyze_live_policy_measurement.py --input docs/evals/2026-10-13-live-policy-measurement`。

[总量 JSON](summary.json)。每个 case 目录含 result、report、model-usage、model-responses（含请求与原始模型文本）、diagnostics；出差目录另含 rag-searches。评测用户和记忆隔离。请求中只有虚构资料，不包含 API 密钥。
