---
name: event-collection
description: Use this skill when the user provides travel details like origin, destination, dates, purpose, or when planning a trip. It extracts structured event information for itinerary planning.
---

# Event Collection Skill

这是一个内部辅助技能，用于从用户输入中提取结构化的行程信息（出发地、目的地、时间等）。

通常由 `IntentionAgent` 自动调度，配合 `plan-trip` 技能使用。

火车、酒店与攻略查询前也须执行。输出出发地、目的地、出发/返程日期、人数 `guests`、酒店入住 `check_in` 与离店 `check_out` 日期。未提供的条件保持 `null`，并列入 `missing_info`；不能默认人数，不能将画像家庭住址作为已确认出发地，也不能自动将旅行日期当作酒店日期。
