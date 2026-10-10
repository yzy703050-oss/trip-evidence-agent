"""
记忆管理器 (Memory Manager)
统一管理两层记忆，提供简单的API
"""
from typing import Dict, Any, List, Optional
from .short_term_memory import ShortTermMemory
from .long_term_memory import LongTermMemory
from .session_store import SessionStore
from .workflow_store import WorkflowStore
from .compaction import estimate_tokens, select_safe_prefix
import logging
import json
import threading
import uuid

logger = logging.getLogger(__name__)


def _model_text(response: Any) -> str:
    content = response.get("content", response) if isinstance(response, dict) else getattr(response, "content", getattr(response, "text", response))
    if isinstance(content, list):
        return "".join(item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text")
    return str(content) if content is not None else ""


async def _read_model_text(response: Any) -> str:
    if hasattr(response, "__aiter__"):
        latest = ""
        async for chunk in response:
            text = _model_text(chunk)
            if text:
                latest = text
        return latest
    return _model_text(response)


class MemoryManager:
    """
    记忆管理器：统一管理两层记忆
    - 短期记忆：最近对话（会话级）
    - 长期记忆：用户偏好和历史（跨会话）
    """

    def __init__(self, user_id: str, session_id: str, storage_path: str = "data/memory", llm_model=None):
        """
        初始化记忆管理器

        Args:
            user_id: 用户ID
            session_id: 会话ID
            storage_path: 长期记忆存储路径
            llm_model: LLM模型实例（用于总结长期记忆）
        """
        self.user_id = user_id
        self.session_id = session_id
        self.llm_model = llm_model

        # 初始化两层记忆
        self.long_term = LongTermMemory(user_id, storage_path)
        self.session_store = SessionStore(storage_path, user_id, session_id)
        self.workflow_store = WorkflowStore(storage_path, user_id)
        self.workflow_store.trip_memory.repair(self.workflow_store)
        self.short_term = ShortTermMemory(max_turns=10)
        self._record_lock = threading.RLock()
        self._cleared_through_seq = self.session_store.read_state().get("cleared_through_seq", 0)
        for event in self.session_store.read_events():
            if event["seq"] > self._cleared_through_seq:
                self.short_term.add_event(event)
        latest_user = next((item for item in reversed(self.session_store.read_events()) if item.get("scope") == "session" and item.get("role") == "user"), None)
        self.current_turn_id = latest_user.get("turn_id") if latest_user else None

        logger.info(f"Memory manager initialized for user {user_id}, session {session_id}")

    def get_active_workflows(self):
        state = self.session_store.read_state()
        ids = state.get('active_workflow_ids', [])
        # A new session may resume this user's unfinished workflow.
        if not ids:
            return self.workflow_store.list_active()
        return [value for workflow_id in ids if (value := self.workflow_store.load(workflow_id)) is not None]

    def get_known_workflows(self, query='', limit=5):
        if type(limit) is not int or limit<1: raise ValueError('invalid known workflow limit')
        self.workflow_store.trip_memory.repair(self.workflow_store)
        values=self.workflow_store.list_all()
        def score(w):
            cities={t.get(k) for t in w['tasks'] for k in ('origin','destination')}
            return sum(bool(city and city in query) for city in cities)+int(w['id'] in query)
        ranked=sorted(values,key=score,reverse=True) if query else values
        ids={w['id'] for w in ranked[:limit]}
        ids.update(w['id'] for w in values if w['status'] in {'running','needs_input','partial'})
        return [w for w in ranked if w['id'] in ids]

    def set_active_workflow(self, workflow_id):
        with self.session_store._lock:
            state = self.session_store.read_state()
            ids = state.get('active_workflow_ids', [])
            if workflow_id is None:
                state.update(active_workflow_ids=[], active_workflow_id=None)
            else:
                if workflow_id not in ids: ids.append(workflow_id)
                state.update(active_workflow_ids=ids, active_workflow_id=workflow_id)
            self.session_store.write_state(state)

    # ========== 短期记忆操作 ==========

    def add_message(self, role: str, content: str, metadata: Dict = None):
        """
        添加消息到短期记忆和长期记忆

        Args:
            role: 角色 (user/assistant)
            content: 消息内容
            metadata: 元数据
        """
        if role == "user":
            return self.start_turn(content, metadata=metadata)
        latest_user = next((item for item in reversed(self.session_store.read_events()) if item.get("scope") == "session" and item.get("role") == "user"), None)
        turn_id = latest_user["turn_id"] if latest_user else uuid.uuid4().hex
        return self.record_message(role, content, turn_id, final=role == "assistant", metadata=metadata)

    def _record_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        with self._record_lock:
            saved = self.session_store.append(event)
            if saved["seq"] > self._cleared_through_seq:
                self.short_term.add_event(saved)
            return saved

    def start_turn(self, user_input: str, metadata: Dict = None) -> str:
        turn_id = uuid.uuid4().hex
        self.current_turn_id = turn_id
        self.record_message("user", user_input, turn_id, metadata=metadata)
        return turn_id

    def record_agent_stage(self, agent_name: str, priority: int, result: Dict[str, Any], turn_id: str = None) -> Dict[str, Any] | None:
        """Direct child calls are execution stages, not model-native tools."""
        active_turn = turn_id or self.current_turn_id
        if not active_turn:
            return None
        return self._record_event({
            "turn_id": active_turn, "scope": "run", "type": "stage_complete",
            "agent_name": agent_name, "priority": priority,
            "status": result.get("status", "unknown"), "content": result,
        })

    def record_message(self, role: str, content: Any, turn_id: str, *, scope: str = "session", final: bool = False, metadata: Dict = None) -> Dict[str, Any]:
        if role not in {"user", "assistant"}:
            raise ValueError("Only user/assistant text messages can be recorded as messages")
        return self._record_event({
            "turn_id": turn_id, "scope": scope, "type": "message", "role": role,
            "content": content, "final": final, "metadata": metadata or {},
        })

    def record_tool_call(self, tool_calls: List[Dict[str, Any]], turn_id: str, scope: str) -> Dict[str, Any]:
        if not tool_calls or not all(call.get("id") and call.get("name") for call in tool_calls):
            raise ValueError("Tool calls require an id and name")
        existing_ids = {call["id"] for event in self.session_store.read_events() for call in event.get("tool_calls", [])}
        ids = [call["id"] for call in tool_calls]
        if len(set(ids)) != len(ids) or any(call_id in existing_ids for call_id in ids):
            raise ValueError("Tool call ids must be unique within a session")
        return self._record_event({
            "turn_id": turn_id, "scope": scope, "type": "tool_call", "role": "assistant",
            "tool_calls": tool_calls,
        })

    def get_pending_tool_calls(self) -> List[Dict[str, Any]]:
        calls = {}
        for event in self.session_store.read_events():
            if event["seq"] <= self._cleared_through_seq:
                continue
            for call in event.get("tool_calls", []):
                calls[call["id"]] = {**call, "turn_id": event["turn_id"], "scope": event["scope"]}
            if event.get("type") == "tool_result":
                calls.pop(event.get("tool_call_id"), None)
        return list(calls.values())

    def record_tool_result(self, tool_call_id: str, content: Any, turn_id: str, scope: str, *, status: str = "success") -> Dict[str, Any]:
        pending = next((call for call in self.get_pending_tool_calls() if call["id"] == tool_call_id), None)
        if pending is None or pending["turn_id"] != turn_id or pending["scope"] != scope:
            raise ValueError("Tool result has no matching pending call")
        return self._record_event({
            "turn_id": turn_id, "scope": scope, "type": "tool_result", "role": "tool",
            "tool_call_id": tool_call_id, "name": pending["name"], "content": content,
            "status": status,
        })

    def record_msg(self, msg: Any, turn_id: str, *, scope: str = "session") -> List[Dict[str, Any]]:
        """Convert AgentScope Msg content blocks into project event records."""
        content = msg.content
        if isinstance(content, str):
            if msg.role in {"user", "assistant"}:
                return [self.record_message(msg.role, content, turn_id, scope=scope)]
            return []
        if not isinstance(content, list):
            return []
        saved = []
        calls = [{"id": block["id"], "name": block["name"], "arguments": block.get("input", {})}
                 for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]
        if calls:
            saved.append(self.record_tool_call(calls, turn_id, scope))
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                saved.append(self.record_tool_result(block["id"], block.get("output"), turn_id, scope,
                                                     status="error" if block.get("is_error") else "success"))
            elif block.get("type") == "text" and block.get("text") and msg.role in {"user", "assistant"}:
                saved.append(self.record_message(msg.role, block["text"], turn_id, scope=scope))
        return saved

    def get_agent_events(self, agent_name: str) -> List[Dict[str, Any]]:
        return [event for event in self.short_term.get_events() if event.get("scope") == f"agent:{agent_name}"]

    def clear_short_term(self) -> None:
        events = self.session_store.read_events()
        self._cleared_through_seq = events[-1]["seq"] if events else 0
        state = self.session_store.read_state()
        state["cleared_through_seq"] = self._cleared_through_seq
        state["summary"] = ""
        state.pop("checkpoint", None)
        self.session_store.write_state(state)
        self.short_term.clear()

    def _compaction_selection(self, token_budget: int, token_counter, keep_recent_turns: int):
        state = self.session_store.read_state()
        covered = max(state.get("covered_through_seq", 0), state.get("cleared_through_seq", 0))
        events = [event for event in self.session_store.read_events() if event["seq"] > covered]
        remaining_budget = max(1, token_budget - max(0, len(state.get("summary", "")) // 2))
        cutoff = select_safe_prefix(events, remaining_budget, token_counter=token_counter,
                                    keep_recent_turns=keep_recent_turns)
        selected = [event for event in events if cutoff is not None and event["seq"] <= cutoff]
        return state, events, cutoff, selected

    def _save_compaction(self, state: Dict[str, Any], events: List[Dict[str, Any]], cutoff: int, selected: List[Dict[str, Any]], summary: str) -> Dict[str, Any]:
        if not summary.strip():
            return state
        state["summary"] = summary.strip()
        state["covered_through_seq"] = cutoff
        if selected and events and cutoff < events[-1]["seq"] and selected[-1].get("turn_id") == events[-1].get("turn_id"):
            latest_user = next((item for item in reversed(selected) if item.get("role") == "user" and item.get("scope") == "session"), None)
            state["checkpoint"] = {
                "active_turn_id": events[-1].get("turn_id"),
                "goal": latest_user.get("content", "") if latest_user else "",
                "completed_stages": [item.get("agent_name") for item in selected if item.get("type") == "stage_complete"],
            }
        else:
            state.pop("checkpoint", None)
        self.session_store.write_state(state)
        return state

    def compact_if_needed(self, token_budget: int, *, summarizer, keep_recent_turns: int = 2,
                          token_counter=estimate_tokens) -> Dict[str, Any]:
        """Compress a structurally selected prefix using an injected summarizer."""
        state, events, cutoff, selected = self._compaction_selection(token_budget, token_counter, keep_recent_turns)
        if cutoff is None:
            return state
        summary = summarizer(state.get("summary", ""), selected)
        return self._save_compaction(state, events, cutoff, selected, summary)

    async def compact_if_needed_async(self, token_budget: int, *, keep_recent_turns: int = 2,
                                      token_counter=estimate_tokens) -> Dict[str, Any]:
        """Use the configured model for free-text summary after selecting a safe cut."""
        state, events, cutoff, selected = self._compaction_selection(token_budget, token_counter, keep_recent_turns)
        if cutoff is None or self.llm_model is None:
            return state
        prompt = (
            "概括以下已完成的对话或执行阶段。保留用户当前目标、硬约束、已确认事实、重要工具结果、"
            "已完成的 Agent 及尚待处理事项。不要添加原文没有的事实。\n"
            f"已有摘要：{state.get('summary', '')}\n"
            f"待压缩事件：{json.dumps(selected, ensure_ascii=False)}\n"
            "请给出简洁摘要："
        )
        try:
            response = await self.llm_model([{"role": "user", "content": prompt}])
            return self._save_compaction(state, events, cutoff, selected, await _read_model_text(response))
        except Exception:
            logger.exception("Could not compact session context; raw events remain available")
            return state

    def get_compacted_context(self, n_turns: int = 5) -> Dict[str, Any]:
        state = self.session_store.read_state()
        covered = max(state.get("covered_through_seq", 0), state.get("cleared_through_seq", 0))
        events = [event for event in self.session_store.read_events() if event["seq"] > covered]
        recent = ShortTermMemory(max_turns=max(10, n_turns))
        for event in events:
            recent.add_event(event)
        summary = state.get("summary", "")
        checkpoint = state.get("checkpoint", {})
        if checkpoint.get("goal"):
            summary = f"【当前任务目标】{checkpoint['goal']}\n{summary}".strip()
        return {"summary": summary, "recent_messages": recent.get_recent_context(n_turns),
                "pending_tool_calls": self.get_pending_tool_calls()}

    # ========== 长期记忆操作 ==========
    # 注意：大部分方法直接使用 self.short_term 和 self.long_term 即可，无需封装

    # ========== 综合查询 ==========

    def get_full_context(self) -> Dict[str, Any]:
        """
        获取完整上下文（两层记忆）

        Returns:
            完整上下文字典
        """
        return {
            "short_term": {
                "recent_dialogue": self.short_term.get_recent_context(5),
                "context_string": self.short_term.get_context_string(5),
                "statistics": self.short_term.get_statistics()
            },
            "long_term": {
                "preferences": self.long_term.get_preference(),
                "chat_history": self.long_term.get_chat_history(10),
                "trip_history": self.long_term.get_trip_history(5),
                "frequent_destinations": self.long_term.get_frequent_destinations(3),
                "statistics": self.long_term.get_statistics()
            }
        }

    def get_context_for_agent(self, long_term_summary: str = None) -> str:
        """
        获取用于Agent的上下文字符串

        Args:
            long_term_summary: 长期记忆总结（可选，需提前调用 get_long_term_summary_async）

        Returns:
            格式化的上下文字符串
        """
        lines = []

        # 长期记忆总结（历史会话）
        if long_term_summary:
            lines.append("【历史会话总结】")
            lines.append(long_term_summary)
            lines.append("")

        # 用户偏好
        prefs = self.long_term.get_preference()
        has_prefs = any(v for v in prefs.values() if v)
        if has_prefs:
            lines.append("【用户偏好】")
            for key, value in prefs.items():
                if value:
                    lines.append(f"- {key}: {value}")
            lines.append("")

        # 短期记忆（当前会话）
        context_str = self.short_term.get_context_string(3)
        if context_str != "无历史对话":
            lines.append("【当前会话对话】")
            lines.append(context_str)
            lines.append("")

        return "\n".join(lines) if lines else "无上下文信息"

    # ========== 会话管理 ==========

    def end_session(self):
        """结束会话"""
        self.short_term.clear()
        logger.info(f"Session ended: {self.session_id}")

    async def get_long_term_summary_async(self, max_messages: int = 50, query: str = "") -> str:
        """Cache one summary per prior session and select relevant sessions."""
        from pathlib import Path

        sessions_dir = Path(self.long_term.storage_path) / self.user_id / "sessions"
        if not sessions_dir.exists():
            return ""
        candidates = []
        for directory in sessions_dir.iterdir():
            if not directory.is_dir() or directory.name == self.session_id:
                continue
            store = SessionStore(self.long_term.storage_path, self.user_id, directory.name)
            visible = [event for event in store.read_events()
                       if event.get("type") == "message" and event.get("scope") == "session"
                       and event.get("role") in {"user", "assistant"}]
            if not visible:
                continue
            state = store.read_state()
            last_seq = visible[-1]["seq"]
            summary = state.get("session_summary", "")
            if not summary or state.get("session_summary_through_seq", 0) < last_seq:
                newer = [event for event in visible if event["seq"] > state.get("session_summary_through_seq", 0)]
                lines = [f"{event['role']}: {event.get('content', '')}" for event in newer[-max_messages:]]
                if self.llm_model:
                    prompt = (
                        "更新这段历史会话的简短摘要。保留用户偏好、已确认事实和重要约束，不要添加没有依据的信息。\n"
                        f"旧摘要：{summary}\n新增消息：{'；'.join(lines)}\n摘要："
                    )
                    try:
                        response = await self.llm_model([{"role": "user", "content": prompt}])
                        summary = (await _read_model_text(response)).strip()
                    except Exception:
                        logger.exception("Could not summarize prior session %s", directory.name)
                if not summary:
                    summary = "；".join(lines[-8:])[-800:]
                state["session_summary"] = summary
                state["session_summary_through_seq"] = last_seq
                store.write_state(state)
            candidates.append((directory.name, summary, visible[-1].get("timestamp", "")))

        if not candidates:
            return ""
        normalized_query = "".join(character.lower() for character in query if character.isalnum())
        grams = {normalized_query[index:index + 2] for index in range(max(0, len(normalized_query) - 1))}
        def score(item):
            normalized_summary = "".join(character.lower() for character in item[1] if character.isalnum())
            return sum(gram in normalized_summary for gram in grams)
        relevant = [item for item in candidates if score(item) > 0]
        if normalized_query:
            selected = sorted(relevant, key=lambda item: (score(item), item[2]), reverse=True)[:3]
        else:
            selected = sorted(candidates, key=lambda item: item[2], reverse=True)[:1]
        return "\n".join(f"【会话 {session_id}】{summary}" for session_id, summary, _ in selected)

    def get_long_term_summary(self, max_messages: int = 50) -> str:
        """
        使用LLM总结长期聊天历史（同步版本）

        Args:
            max_messages: 最多总结的消息数量

        Returns:
            总结后的文本
        """
        import asyncio

        # 检查是否在事件循环中
        try:
            loop = asyncio.get_running_loop()
            # 已经在事件循环中，不能使用 asyncio.run
            logger.warning("get_long_term_summary called from async context, please use get_long_term_summary_async instead")
            return ""
        except RuntimeError:
            # 没有运行的事件循环，可以使用 asyncio.run
            return asyncio.run(self.get_long_term_summary_async(max_messages))
