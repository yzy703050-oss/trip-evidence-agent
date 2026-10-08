---
name: plan-trip
description: Use this skill when the user wants to plan a trip or asks for itinerary planning. Triggers when user says "规划行程", "安排路线", "我要去XX", "从XX到XX", or provides trip details like dates and destinations. This skill orchestrates IntentionAgent, EventCollectionAgent, and ItineraryPlanningAgent; all agents take model=model and are async.
---

# Plan Trip (行程规划)

为用户规划出行行程：意图识别 → 事项收集（出发地、目的地、日期等）→ 行程规划。所有 Agent 均使用 **model 对象**，且 **reply() 均为 async**。

## When to Use

- 用户说「规划行程」「从XX到XX」「X月X日去北京」等

## Agents（按顺序）

1. **IntentionAgent** — 识别意图与改写 query  
2. **EventCollectionAgent** — 提取出发地、目的地、日期、目的等  
3. **ItineraryPlanningAgent** — 生成行程（每日安排、交通、住宿建议等）

## 统一模型与异步

- 先创建 `OpenAIChatModel`（来自 `config.LLM_CONFIG`），再传给各 Agent 的 **model** 参数（本项目无 `model_config_name`）。
- 三个 Agent 的 `reply()` 都是 **async**，需 **await**。

## 调用示例（简化链式）

```python
import asyncio
import json
from agentscope.message import Msg
from agentscope.model import OpenAIChatModel
from config_agentscope import init_agentscope
from config import LLM_CONFIG
from agents.intention_agent import IntentionAgent
from agents.event_collection_agent import EventCollectionAgent
from agents.itinerary_planning_agent import ItineraryPlanningAgent

async def plan_trip(user_query: str):
    init_agentscope()
    model = OpenAIChatModel(
        model_name=LLM_CONFIG["model_name"],
        api_key=LLM_CONFIG["api_key"],
        client_kwargs={"base_url": LLM_CONFIG["base_url"], "timeout": 60},
        temperature=LLM_CONFIG.get("temperature", 0.7),
        max_tokens=LLM_CONFIG.get("max_tokens", 2000),
    )
    user_msg = Msg(name="user", content=user_query, role="user")

    # 1. 意图识别
    intention_agent = IntentionAgent(name="IntentionAgent", model=model)
    intention_result = await intention_agent.reply(user_msg)
    intention_data = json.loads(intention_result.content)
    rewritten_query = intention_data.get("rewritten_query", user_query)

    # 2. 事项收集（传入 context 格式，与 OrchestrationAgent 一致）
    context = {"rewritten_query": rewritten_query, "user_preferences": {}}
    event_input = Msg(name="Orchestrator", content=json.dumps({"context": context}), role="user")
    event_agent = EventCollectionAgent(name="EventCollectionAgent", model=model)
    event_result = await event_agent.reply(event_input)
    event_data = json.loads(event_result.content) if isinstance(event_result.content, str) else event_result.content

    # 3. 行程规划（传入 previous_results，包含 event_collection 结果）
    previous_results = [{"agent_name": "event_collection", "data": event_data}]
    plan_input = Msg(
        name="Orchestrator",
        content=json.dumps({"context": context, "previous_results": previous_results}, ensure_ascii=False),
        role="user",
    )
    plan_agent = ItineraryPlanningAgent(name="ItineraryPlanningAgent", model=model)
    plan_result = await plan_agent.reply(plan_input)
    plan_data = json.loads(plan_result.content) if isinstance(plan_result.content, str) else plan_result.content
    return plan_data

# 使用
result = asyncio.run(plan_trip("规划一下2月27日从上海到北京的路程"))
# result: {"itinerary": {"title", "duration", "route", "daily_plans", "notes", ...}, "planning_complete": bool}
```

## EventCollectionAgent 输出字段（示例）

- `origin`, `destination`, `start_date`, `end_date`, `duration_days`, `trip_purpose`, `missing_info` 等

## ItineraryPlanningAgent 输出字段（示例）

- `itinerary`: `title`, `duration`, `route`, `daily_plans`, `notes`, `estimated_budget` 等
- `planning_complete`: bool

## 错误与缺失信息

- 若意图解析非 JSON，可提示用户重新描述。
- 若 `event_data` 含 `missing_info`，可提示用户补全再继续。


## 行程规划 Prompt 指南

只生成安排建议：每日日期、时间、活动地点。火车和酒店选择必须引用 previous_results 中的报价 ID（selected_train_id、selected_hotel_id）；不存在可用候选项时返回 null。
价格、车次、房型、库存、取消规则、天气、开放时间与预约规则均由程序从对应查询结果重建。不得根据季节、常识或网页摘要补齐这些事实，不生成 estimated_budget 或预算内保证。
输出 JSON：{"itinerary":{"daily_plans":[{"day":1,"date":"2026-10-08","activities":[{"time":"09:00-12:00","location":"故宫"}]}]},"selected_train_id":null,"selected_hotel_id":null,"planning_complete":true}。
活动时间仅是安排建议；实际开放与预约仍须查看有来源的攻略事实，未知信息保持待核实。
