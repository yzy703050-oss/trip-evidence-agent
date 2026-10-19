---
name: preference
description: 使用原生工具读取和更新明确偏好，独立Agent负责每三份行程的增量总结。
---

用户明确表达长期偏好时，主Agent用 `write_memory` 表达追加、定向替换或撤回，保留条件和强度；工具直接保存，无额外偏好分析模型。读取用 `read(resource=preferences)`；本次日期、人数、预算不能自动当长期偏好。目标不清先澄清。

简单写仅更新长期记忆，不能修改TripPlan。混合请求先真实保存并重读，再规划；写入失败不使用未生效偏好。明确CLI命令可无模型执行，所有模型发起工具的结果仍回主Agent答复。

`trip_assistant.agents.preference` 的独立Agent首次累计3份用户确认且校验保存成功的唯一有效行程后总结，此后每新增3份结合旧总结更新。失败、取消、模拟行程不计；修改/恢复不重复计。总结、证据、覆盖ID集合与记忆原子保存，失败不推进。政策和助手推荐不能充当用户偏好，明确记忆和撤回保护优先。

用户明确要求重分析时用 `write_memory(operation=reanalyze)` 委派同一独立Agent；普通读写不重分析或重置覆盖状态。仓库实现见 `trip_assistant/storage/preferences.py`，不调用旧MemoryManager或PreferenceAgent接口。
