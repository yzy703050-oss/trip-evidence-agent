## Why

上海简单行程实际调用模型12次，记录合计50,707 tokens，存在识别与派发分离、子模型重复生成命令、逐动作收尾和重复回复组织。用户最终要求把偏好提取及火车酒店查询能力合并到主Agent，工具结果直接返回主Agent，作为下一阶段开发方向。

## What Changes

- 用户将原后续改进一并纳入当前实施：完全移除AgentScope、改官方OpenAI SDK及项目协议；增加TripPlan只读详情与已保存候选分页，不触发外部查询或业务修改。

- WorkflowRunner提升为整个请求的唯一程序编排器，统一前置资料、模型调用、任务调度、三层状态推进和局部修复；ExecutionHarness保留入口兼容并转交，不再独立编排。工具执行、隔离并发、单工具超时与程序清洗留在工具层，不新增编排模型。
- 偏好更新/读取与历史读取改为无模型工具，更新最高优先；旧preference/memory_query模型能力合并为首次达到3份有效行程才总结一次的偏好分析Agent，保存完成后不再自动重复；明确请求重新分析历史才再次调用。
- 政策Agent继承rag_knowledge检索，负责理解政策、适用条件和例外，直接提取带来源的强制约束JSON；主Agent使用约束规划，不重复从检索原文提取。
- information_query静默保留设计和代码，本阶段全部停止调度；确定查询由工具执行，无须查询子模型。
- 火车/酒店/天气的独立请求并行执行，保持priority与depends_on屏障和独立作用域。
- 火车/酒店/天气工具复用现有程序清洗，并补齐去重、领域白名单、分页与文本限长，保留温度/日期/来源/未核实约束；结果按任务或请求身份直接返回主Agent，不调用查询总结模型。
- 主Agent一次提交多任务选择、草稿和回复模板，程序自动全程校验及展示。
- 后续修改在主意图识别中合并定位TripPlan/任务/组件，已有信息足够时直接提交受控修改或候选重组；程序先复用完整有效缓存与政策，只在缺证据或需要刷新时补查询。改选失效与查询失效分开，复杂依赖由程序自动检查。
- 三层状态简化加入立即修改：TripPlan仅running/completed，task仅pending/draft/validated，查询容器仅empty/ready/failed；needs_input、依赖等待及请求在途从既有记录派生，失效结果解除当前绑定并回empty，旧数据/停止语义兼容。
- 保留schedule多段任务与任务标签；各task具有按领域划分的查询容器，程序根据当前结果有效性、条件与执行记录执行/复用查询，逐task形成查询草稿，全部就绪后一次交主Agent判断。冲突时模型选择修复任务和组件，程序局部失效与依赖更新，不全量重跑。
- 简单上海行程目标2次主模型调用；纯偏好读写0次模型；偏好分析默认首次3份有效行程总结一次；记忆为可增量的自然语言条件句，明确更新立即保存且不触发分析。复杂依赖及冲突按需追加决策。
- 保留手动偏好保护、临时条件边界、缓存、分页、恢复、版本与审计；专业子Agent按需保留。
- **BREAKING（内部协议）**：正常路径不再调度旧preference/memory_query/information_query子模型，调整初始化、批量动作和三层持久化状态契约。旧持久化用户数据继续非破坏性兼容，暂停/取消原因不丢失。

## Capabilities

### New Capabilities

- `project-model-runtime`: 官方SDK与项目轻量消息/Agent协议，完整响应及真实用量。
- `saved-trip-read-only`: 目标定位后只读详情、有效/历史候选分页与隔离。

- `simplified-trip-state-machine`: 立即简化三层业务状态，保留派生原因、受控转移和兼容恢复。
- `consolidated-travel-execution`: 主Agent调度、程序查询、结果直接返回及合并收尾。
- `main-agent-preference-updates`: 明确偏好无模型读写、程序应用和真实生效确认。
- `history-preference-analysis`: 首次历史总结、持久化完成标记、条件性自然语言软记忆及特殊重分析。
- `travel-policy-preflight`: 检索理解、带来源JSON约束提取及政策前置。
- `saved-trip-amendment`: 修改意图定位、已有信息优先、受控patch及按需查询/依赖修复。
- `bounded-travel-tool-results`: 无模型清洗、领域有界视图、必要事实与来源缺口保留。
- `priority-tool-scheduling`: 前置优先级、独立查询并行及作用域隔离。

### Modified Capabilities

- `progressive-context-disclosure`: 有界批量结果直接供主模型判断，支持合并动作。
- `agent-context-isolation`: 主模型可接收程序工具投影，无须为火车酒店增设子模型摘要。

## Impact

涉及main/contracts、Harness/Runner、程序工具执行和投影、WorkflowStore与小结/活动发现/恢复、偏好持久化及有效历史样本接入、懒加载注册、测试及能力文档。复用现有存储与供应商接口，不新增框架依赖。历史读取改工具，政策Agent扩展理解与JSON约束提取，检索底层沿用现有设计；工具清洗和投影补齐已发现的边界缺口。

## Non-goals

不改变默认住宿与返程产品规则，不新增购票订房，不移除校验，不删除information_query保留代码、不全面重构RAG底层。不承诺固定token节省比例。
