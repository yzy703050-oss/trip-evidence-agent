---
name: ask-question
description: 通过保留RAG取得有来源的政策材料，由独立政策Agent解释。
---

企业出差规划由主Agent调用 `query_policy` 委派独立政策Agent，正式TripPlan前取得理解小结及可校验约束，包含来源、适用条件、例外和缺口。普通旅游与纯历史读取不自动检索；修改可复用仍有效政策，城市、身份、项目或日期变化时重核。

RAG检索资源继续保留在本目录的script、知识库配置与既有数据路径；运行适配器为 `trip_assistant/providers/rag.py`，政策校验为 `trip_assistant/agents/policy.py`。不得重建知识库或迁走用户数据作为普通应用启动的一部分。

政策原文和子调用记录留审计，主Agent只接收必要小结、结构约束与引用。缺材料不能当无限制，也不能根据模型常识虚构审批或政策通过。所有政策工具结果回主Agent，不使用旧agentscope初始化或正文JSON动作路由。

需要用户明确维护知识库时可运行本目录script/init_knowledge_base.py；该脚本是知识库维护入口，不由正常规划自动触发。
