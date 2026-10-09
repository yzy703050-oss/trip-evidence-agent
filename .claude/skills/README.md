# V0 Skill 子 Agent

V0 命令行入口是仓库根目录的 `cli.py`。用户输入自然语言后，`IntentionAgent` 生成 `agent_schedule`；`OrchestrationAgent` 按优先级调用以下 Skill 的 `script/agent.py`。注册器首次使用时加载并在当前进程缓存 Agent。`SKILL.md` 提供意图说明及部分 Agent 的提示词，不会自行执行脚本。

| Skill 目录 | 调度名称 | 职责 |
| --- | --- | --- |
| `event-collection` | `event_collection` | 提取出发地、目的地、日期及事项。 |
| `preference` | `preference` | 提取用户偏好，由调度器保存。 |
| `memory-query` | `memory_query` | 读取用户偏好、历史行程及相关摘要。 |
| `ask-question` | `rag_knowledge` | 检索虚构企业的演示政策文档并回答。 |
| `query-info` | `information_query` | 查询天气或进行通用网页搜索。 |
| `plan-trip` | `itinerary_planning` | 根据已收集信息生成行程建议。 |
| `train-search` | `train_search` | 查询带来源的火车报价与库存。 |
| `hotel-search` | `hotel_search` | 查询带来源的酒店房型总价与库存。 |
| `travel-guide` | `travel_guide` | 查询逐项保留来源的攻略事实。 |

三个查询 Agent 通过注入的授权 Provider 取得数据，默认返回 unavailable，不生成演示报价。缺少条件返回 needs_input；来源和查询时间缺失时明确保留 null。实际平台接入尚待授权。运行和测评方法见根目录 `README.md`。
