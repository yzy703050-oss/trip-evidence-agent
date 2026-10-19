---
name: memory-query
description: 主Agent按需读取本用户偏好、行程摘要及会话历史。
---

使用原生 `read`，无需独立MemoryQueryAgent或MemoryManager。

长期偏好用preferences；行程先trip_summaries摘要定位，可按城市、日期、目的、会话关联筛选；目标多义先澄清，再按trip_plan_id精读trip。新规划不自动继承旧路线和待办。

回忆其他会话时先session_summaries，再按session_id读session，按页返回主消息与摘要，不注入子Agent审计；不存在的ID不创建会话，也不能访问其他用户。

缓存查询用candidates的query_id/result_revision/offset，保持原筛选和排序，仅按有效下一页读取；不发供应商请求、不改变行程状态。历史证据带时间，不能当成当前实时结果。所有读结果回主Agent组织答复。schema见 `trip_assistant/tools/registry.py`。
