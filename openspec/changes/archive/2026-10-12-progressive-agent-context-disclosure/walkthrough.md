# 上下文与多段旅行流程示例

本文解释 design.md，行为以 specs 为准。以下车次、日期和价格是演示数据。

## 定位和恢复

用户说“继续上次上海的旅程”。程序先读取用户目录独立的 workflow-index.json，得到相关旅程的 ID、版本、状态、路线、日期和小结；正常定位不打开各个完整 workflow JSON。主模型在一次初始化请求中同时识别恢复意图并输出 resume_workflow_id。有歧义时走原有澄清协议。

程序随后只加载选中的 workflows/<id>.json，核对所有权、版本和 checkpoint，再组装精简的当前任务视图供后续决策。文件读取本身不调用模型：意图及目标选择一次、后续任务判断一次通常是两次请求，并非固定三次。子查询和最终答复另计。索引首次缺失或损坏允许一次旧数据重建；正常保存增量更新对应项，trip.md 从索引渲染。

## 新行程、委派和候选

用户说“20日北京去上海住两晚，再去杭州住一晚，最后回北京”。主模型提出三个路段；程序生成任务身份、条件、依赖、版本和结果容器。默认返程保持既有规则。主模型看到轻量全程任务概览、唯一当前任务详情和前段边界，输出 dispatch JSON。

Runner 校验后委派子 Agent。子上下文只有原需求、当前 task/query_requests、必要条件与偏好、preflight 和前段边界，不附主会话历史、全部任务及前次调用的工具消息。子 Agent 独立执行“模型→查询工具→模型”；下次调用重新构造上下文。

完整查询候选、网址、坐标、原始来源及证据仍保存到 workflow JSON 和 candidate_cache。主模型收到子小结、查询身份/版本/缺口，在 relevant_results 一处看到当前页的必要候选事实。例如：

```json
{"id":"query_1","domain":"train","result_revision":1,"source":{"provider":"simulation:train"},"items":[{"id":"candidate_1","train_number":"G1","departure_at":"2026-10-20T08:00:00+08:00","arrival_at":null,"arrival_time":"12:00","price_cny":"100","seat_class":"二等座","availability":"available"}]}
```

arrival_at=null 始终表示抵达日期未知。酒店地点保留 hotel_place 类型、名称和地址，不伪装成有报价/库存的房间；评分、距离、取消条件仅实际需求需要时暴露。网址、坐标、原始证据、重复查询条件和重复时刻不默认进入主提示词。

本次沿用状态机和已存在的候选分页/缓存：需要后续候选时 dispatch 带 candidate_offset，子工具从缓存返回下一页；随后主模型按引用选择草稿。没有新增主原生只读工具，也没有额外的“先选读取工具、再读详情、再决策”循环。缓存复用本身不访问外部供应商；refresh 等现有规则保持。

## 草稿、跨段冲突与修复

每段 draft_task 保存事实引用与小结，再处理下一段。所有段齐全后 validate_workflow，由程序核验引用、时间、路线、人数及预算。主模型最终通过 reply_facts 模板表达，不能编造事实。

若第二段出发早于第一段离店，主模型可选择第二段，而不是固定重做最后一段：

```json
{"action":"repair_tasks","workflow_revision":12,"task_ids":["task_2"],"components":["train"],"conflict_codes":["departure_before_previous_checkout"],"reason":"更换第二段交通以满足前段住宿"}
```

Runner 核对真实阻塞问题及身份/版本，重开根任务，仅清除受影响组件与可重新推导的建议日期，保留用户固定晚数/日期、有效酒店引用和完整历史。checkpoint 保存根任务、边界及待重验依赖；根任务完成后程序按路线顺序检查依赖，仍有效的草稿保留，失效的段进入重算。恢复同一 checkpoint 后继续该流程。重复修复无进展会停止；修复完成仍须重新全程校验。

## 记忆与额度

workflow JSON 是完整权威业务状态；workflow-index.json/trip.md 是可重建摘要。主会话仅保存用户/最终回复并按 token 压缩完整旧用户轮次，子调用过程留在该次上下文与执行审计。runs 记录真实请求、合法调度和成功委派，events 可追踪 JSON 决定和执行。

全部功能完成后运行 scripts/calibrate_resources.py：解除待校准的应用层额度，使用明确标注的离线模型/数据替身。分别记录同次上下文、完整输出、主 turn/任务模型请求、子单委派模型请求、实际调度和辅助委派；最终配置各取相同作用域成功最大值两倍，失败和熔断排除。报告注明估算、来源和未做真实供应商/模型验证。工具、超时、候选分页和熔断保持独立；不进行用户豁免的单 turn 超限停止/分段提示专项测试。
