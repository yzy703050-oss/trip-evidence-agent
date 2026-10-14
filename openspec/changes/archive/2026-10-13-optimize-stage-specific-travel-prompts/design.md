## Context

首次initialize提示将完整schema、例子和自然语言规则重复展开；select_batch规则与batch_query_contract重复，query_tools始终包含四个查询schema。完整候选已缓存、有界投影存在，本次不进一步删候选。

## Decisions

1. 新增agents/travel_prompts.py统一首次与批量提示。根据可观测已有行程/等待上下文加载历史操作；不使用关键词猜用户意图、不增加路由模型。未知目标的修改请求仍澄清。
2. workflow_contract_prompt增加compact参数：正常首次使用字段类型、枚举、层级和一个小例子；原完整schema与程序校验保留，修正阶段继续可使用完整说明。
3. 新增模型边界batch上下文裁剪：不改Runner内部工具清单或allowed_actions；仅提交时移除query_tools，提供query_capabilities。查询开放时只省略不涉及的weather和未使用的arrival工具；保留分页参数、实际schema及能力。对上下文深拷贝，原数据不变。
4. 合并选择规则，保留酒店地点可推荐、去程失败保留后段、硬/软日期、政策/预算、成功空结果可调整soft日期、非法冲突代码限制。回复模板可省略，避免为生成正文增加模型调用。
5. 实施前冻结旧提示字符串，使用同一模型配置、时间、模拟原始API夹具及独立记忆做一组完整基线与优化比较；另外跑固定日期、多段、去程失败、仅换酒店。保存实际数据回放与政策/日期修复回放作为复杂行为证据，不修改用户历史DB。
6. 首轮真实评估发现短契约遗漏的重要边界，保留修正：明确confirmed_conditions也只用条件字段，单段日期不得全局继承；目的不明用unspecified，不能默认为business；返程destination使用可信home_location，不能null；departure_date与全程起止边界区分。恢复这些规则后不再触发首次结构修复。首轮失败记录保留，不用token下降代替正确性。

## Risks / Validation

提示过短可能失去边界或导致额外修正；必须检查实际最终答复和修改范围。程序投影TDD先确认提交阶段无schema、查询阶段仍可分页、原输入不变；提示内容不以文本断言代替模型行为。模型阶段每请求最多4次，真实供应商调用为0，不自动重试。检查全套pytest、ruff、自审、OpenSpec严格校验；CLI无verify则人工逐项核对。用户已选择单代理当前目录，不重复询问。
