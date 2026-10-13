# API形状一致的模拟验收与消耗记录

真实模型为项目配置的 deepseek-flash；政策通过真实本地向量RAG检索虚构制度，embedding在本地运行。供应商HTTP全部模拟，没有重试真实聚合/高德网络接口。

火车注入聚合error_code/result/prices原始响应，复用生产价格、余票与时长解析。高德注入status/infocode/pois原始响应，复用POI解析、去重及坏坐标排除；business.cost不是房价，住宿价格、房型、库存保持未知。普通出差及冲突场景的酒店使用独立HotelOffer报价契约夹具，不能解释为高德房价。

| 场景 | 模型调用 | 输入token | 输出token | 合计 | 请求延时 | 结果 | 验收 |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| 普通旅游：聚合/高德原始响应 | 2 | 6,788 | 727 | 7,515 | 5.068s | completed | 通过 |
| 普通出差：聚合原始响应＋独立报价夹具 | 3 | 9,522 | 1,349 | 10,871 | 7.700s | completed | 通过 |
| 政策冲突：聚合原始响应＋超标报价重查 | 4 | 15,140 | 1,512 | 16,652 | 8.277s | completed | 通过 |
| 报价能力缺口：聚合/高德原始响应 | 3 | 9,530 | 1,461 | 10,991 | 7.672s | partial | 通过 |

以上四个验收记录共 **46,029 token / 12 次模型调用**。
旅游为主规划＋批量选择；普通出差多一次政策提取；冲突多一次酒店重查决定。两段火车均只查一次，超标酒店由700/760元每晚刷新为380/420元每晚，按当前500元上限验证完成；旧900元制度和无审批800元例外没有放宽额度。

`partial` 的高德场景表示任务未完成，但缺报价被如实保留且验收符合预期。程序完整保存/校验后按已知能力缺口结束；不再依赖模型填写allow_repair=false。混合火车、时间、未知政策冲突仍走原有限修复。

请求延时从进入WorkflowRunner到返回计算，包含模型/检索/工具执行，不包含RAG首次初始化及入库；每次模型的TTFT/耗时/usage见原始记录。这是每场景单次测量，不能据此给出稳定性或延时分位数保证。

## 未通过轮次与全部消耗

第一次纯高德模拟正确返回partial，但5调用24,449 token后no_progress，验收未通过。该记录原样保留；定向修复后只重测此场景，没有重跑已通过的其他三场景。普通出差旧报告曾将调用数未达标计入task_success；新报告独立记录task_success与optimization_success，不回写历史记录。

| 记录目录 | 模型调用 | 输入token | 输出token | 合计 |
| --- | ---: | ---: | ---: | ---: |
| [2026-10-13-api-shaped-capability-fixed](../2026-10-13-api-shaped-capability-fixed/summary.json) | 3 | 9,530 | 1,461 | 10,991 |
| [2026-10-13-api-shaped-simulation](../2026-10-13-api-shaped-simulation/summary.json) | 14 | 54,310 | 5,177 | 59,487 |
| [2026-10-13-live-model-simulated-suppliers](../2026-10-13-live-model-simulated-suppliers/summary.json) | 6 | 21,822 | 1,726 | 23,548 |
| [2026-10-13-live-model-verified](../2026-10-13-live-model-verified/summary.json) | 11 | 39,396 | 3,579 | 42,975 |
| [2026-10-13-live-policy-batch-fixed](../2026-10-13-live-policy-batch-fixed/summary.json) | 4 | 13,843 | 889 | 14,732 |
| [2026-10-13-live-policy-final-conflict](../2026-10-13-live-policy-final-conflict/summary.json) | 4 | 14,067 | 1,078 | 15,145 |
| [2026-10-13-live-policy-fixed](../2026-10-13-live-policy-fixed/summary.json) | 17 | 63,747 | 5,216 | 68,963 |
| [2026-10-13-live-policy-measurement](../2026-10-13-live-policy-measurement/summary.json) | 8 | 9,043 | 4,408 | 13,451 |
| [2026-10-13-live-policy-verified-conflict](../2026-10-13-live-policy-verified-conflict/summary.json) | 3 | 9,180 | 1,247 | 10,427 |
| [2026-10-13-live-policy-verified-final](../2026-10-13-live-policy-verified-final/summary.json) | 9 | 31,250 | 2,987 | 34,237 |

本次API形状模拟（含失败案例及定向重测）实际共 **70,478 token**。全部已保存的十轮测量共 **293,956 token / 79 次调用**，包含失败和修复轮次，不能只报最终通过的46,029 token，也不能把累计调试量当成单次行程消耗。
这是SDK报告的输入＋输出使用量；未据此计算费用，缓存折扣/价格及账单额度未核对。

## 逐次记录及模拟数据

- 普通旅游：聚合/高德原始响应：[原始响应及生产解析结果](../2026-10-13-api-shaped-simulation/tourism_simulated/supplier-fixtures.json)、[模型请求与输出/TTFT](../2026-10-13-api-shaped-simulation/tourism_simulated/model-responses.json)、[token/延时/验收](../2026-10-13-api-shaped-simulation/tourism_simulated/report.json)、[程序校验](../2026-10-13-api-shaped-simulation/tourism_simulated/validation.json)。
- 普通出差：聚合原始响应＋独立报价夹具：[原始响应及生产解析结果](../2026-10-13-api-shaped-simulation/business_simulated/supplier-fixtures.json)、[模型请求与输出/TTFT](../2026-10-13-api-shaped-simulation/business_simulated/model-responses.json)、[token/延时/验收](../2026-10-13-api-shaped-simulation/business_simulated/report.json)、[程序校验](../2026-10-13-api-shaped-simulation/business_simulated/validation.json)。
- 政策冲突：聚合原始响应＋超标报价重查：[原始响应及生产解析结果](../2026-10-13-api-shaped-simulation/business_conflict/supplier-fixtures.json)、[模型请求与输出/TTFT](../2026-10-13-api-shaped-simulation/business_conflict/model-responses.json)、[token/延时/验收](../2026-10-13-api-shaped-simulation/business_conflict/report.json)、[程序校验](../2026-10-13-api-shaped-simulation/business_conflict/validation.json)。
- 报价能力缺口：聚合/高德原始响应：[原始响应及生产解析结果](../2026-10-13-api-shaped-capability-fixed/business_api_simulated/supplier-fixtures.json)、[模型请求与输出/TTFT](../2026-10-13-api-shaped-capability-fixed/business_api_simulated/model-responses.json)、[token/延时/验收](../2026-10-13-api-shaped-capability-fixed/business_api_simulated/report.json)、[程序校验](../2026-10-13-api-shaped-capability-fixed/business_api_simulated/validation.json)。

## 验证

TDD复现原始响应模拟缺失、到达查询工具绑定和能力缺口重复调用后修复；相关回归实际执行。全量测试、Ruff及规格核对证据见[自审记录](../../superpowers/reviews/2026-10-13-live-model-contracts-verification.md)。
