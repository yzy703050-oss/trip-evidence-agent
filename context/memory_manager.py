"""
记忆管理器 (Memory Manager)
统一管理两层记忆，提供简单的API
"""

from context.context_budget import ContextBudgetExceeded, budget_scope
from context.resource_budget import ModelCallLimitExceeded
from typing import Dict, Any, List, Optional
from .short_term_memory import ShortTermMemory
from .long_term_memory import LongTermMemory
from .session_store import SessionStore
from .workflow_store import WorkflowStore
from .compaction import estimate_tokens, select_safe_prefix, complete_prefix_boundaries
from agents.context_projection import project_session_message
import logging
import json
import threading
import uuid

logger = logging.getLogger(__name__)


def _model_text(response: Any) -> str:
    content = (
        response.get("content", response)
        if isinstance(response, dict)
        else getattr(response, "content", getattr(response, "text", response))
    )
    if isinstance(content, list):
        return "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
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

    def __init__(
        self, user_id: str, session_id: str, storage_path: str = "data/memory", llm_model=None
    ):
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
        self.short_term = ShortTermMemory()
        self._record_lock = threading.RLock()
        state = self.session_store.read_state()
        self._cleared_through_seq = state.get("cleared_through_seq", 0)
        visible = self._main_events(state)
        for event in visible:
            self.short_term.add_event(event)
        latest_user = next((item for item in reversed(visible) if item.get("role") == "user"), None)
        self.current_turn_id = latest_user.get("turn_id") if latest_user else None

        logger.info(f"Memory manager initialized for user {user_id}, session {session_id}")

    def get_active_workflows(self):
        state = self.session_store.read_state()
        ids = state.get("active_workflow_ids", [])
        return [
            value
            for workflow_id in ids
            if (value := self.workflow_store.load(workflow_id)) is not None
        ]

    @staticmethod
    def references_history(query):
        return any(
            word in query
            for word in (
                "上次",
                "之前",
                "以前",
                "历史",
                "旧行程",
                "继续",
                "恢复",
                "重试",
                "上个",
                "那趟",
                "那次",
                "这段",
                "那段",
                "workflow_",
            )
        )

    def get_workflow_context(self, query=""):
        if self.references_history(query):
            return self.get_known_workflows(query=query)
        ids = set(self.session_store.read_state().get("active_workflow_ids", []))
        return [w for w in self.workflow_store.list_summaries() if w["id"] in ids]

    def get_known_workflows(self, query="", limit=5):
        if type(limit) is not int or limit < 1:
            raise ValueError("invalid known workflow limit")
        self.workflow_store.trip_memory.repair(self.workflow_store)
        values = self.workflow_store.list_summaries()

        def score(w):
            cities = {t.get(k) for t in w["tasks"] for k in ("origin", "destination")}
            return sum(bool(city and city in query) for city in cities) + int(w["id"] in query)

        ranked = sorted(values, key=score, reverse=True) if query else values
        ids = {w["id"] for w in ranked[:limit]}
        ids.update(w["id"] for w in values if w["status"] in {"running", "needs_input", "partial"})
        return [w for w in ranked if w["id"] in ids]

    def set_active_workflow(self, workflow_id):
        if workflow_id is None:
            self.session_store.update_state({"active_workflow_ids": [], "active_workflow_id": None})
        else:
            self.session_store.add_active_workflow(workflow_id)

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
        turn_id = self.session_store.latest_user_turn() or uuid.uuid4().hex
        return self.record_message(
            role, content, turn_id, final=role == "assistant", metadata=metadata
        )

    def _record_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        with self._record_lock:
            saved = self.session_store.append(event)
            state = self.session_store.read_state()
            covered = max(state.get("covered_through_seq", 0), state.get("cleared_through_seq", 0))
            view = project_session_message(saved)
            if saved["seq"] > covered and view is not None:
                self.short_term.add_event(view)
            return saved

    def start_turn(self, user_input: str, metadata: Dict = None) -> str:
        turn_id = uuid.uuid4().hex
        self.current_turn_id = turn_id
        self.record_message("user", user_input, turn_id, metadata=metadata)
        return turn_id

    def record_agent_stage(
        self, agent_name: str, priority: int, result: Dict[str, Any], turn_id: str = None
    ) -> Dict[str, Any] | None:
        """Direct child calls are execution stages, not model-native tools."""
        active_turn = turn_id or self.current_turn_id
        if not active_turn:
            return None
        return self._record_event(
            {
                "turn_id": active_turn,
                "scope": "run",
                "type": "stage_complete",
                "agent_name": agent_name,
                "priority": priority,
                "status": result.get("status", "unknown"),
                "content": result,
            }
        )

    def record_message(
        self,
        role: str,
        content: Any,
        turn_id: str,
        *,
        scope: str = "session",
        final: bool = False,
        metadata: Dict = None,
    ) -> Dict[str, Any]:
        if role not in {"user", "assistant"}:
            raise ValueError("Only user/assistant text messages can be recorded as messages")
        return self._record_event(
            {
                "turn_id": turn_id,
                "scope": scope,
                "type": "message",
                "role": role,
                "content": content,
                "final": final,
                "metadata": metadata or {},
            }
        )

    def record_tool_call(
        self, tool_calls: List[Dict[str, Any]], turn_id: str, scope: str
    ) -> Dict[str, Any]:
        if not tool_calls or not all(call.get("id") and call.get("name") for call in tool_calls):
            raise ValueError("Tool calls require an id and name")
        ids = [call["id"] for call in tool_calls]
        if len(set(ids)) != len(ids):
            raise ValueError("Tool call ids must be unique within a session")
        return self._record_event(
            {
                "turn_id": turn_id,
                "scope": scope,
                "type": "tool_call",
                "role": "assistant",
                "tool_calls": tool_calls,
            }
        )

    def get_pending_tool_calls(self) -> List[Dict[str, Any]]:
        state = self.session_store.read_state()
        return self.session_store.pending_tool_calls(after_seq=state.get("cleared_through_seq", 0))

    def record_tool_result(
        self, tool_call_id: str, content: Any, turn_id: str, scope: str, *, status: str = "success"
    ) -> Dict[str, Any]:
        pending = next(
            (call for call in self.get_pending_tool_calls() if call["id"] == tool_call_id), None
        )
        if pending is None or pending["turn_id"] != turn_id or pending["scope"] != scope:
            raise ValueError("Tool result has no matching pending call")
        return self._record_event(
            {
                "turn_id": turn_id,
                "scope": scope,
                "type": "tool_result",
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": pending["name"],
                "content": content,
                "status": status,
            }
        )

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
        calls = [
            {"id": block["id"], "name": block["name"], "arguments": block.get("input", {})}
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        if calls:
            saved.append(self.record_tool_call(calls, turn_id, scope))
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                saved.append(
                    self.record_tool_result(
                        block["id"],
                        block.get("output"),
                        turn_id,
                        scope,
                        status="error" if block.get("is_error") else "success",
                    )
                )
            elif (
                block.get("type") == "text"
                and block.get("text")
                and msg.role in {"user", "assistant"}
            ):
                saved.append(self.record_message(msg.role, block["text"], turn_id, scope=scope))
        return saved

    def get_agent_events(self, agent_name: str) -> List[Dict[str, Any]]:
        state = self.session_store.read_state()
        return self.session_store.read_events(
            after_seq=state.get("cleared_through_seq", 0), scope=f"agent:{agent_name}"
        )

    def clear_short_term(self) -> None:
        state = self.session_store.clear_context()
        self._cleared_through_seq = state["cleared_through_seq"]
        self.short_term.clear()

    def _main_events(self, state):
        covered = max(state.get("covered_through_seq", 0), state.get("cleared_through_seq", 0))
        return [
            view
            for event in self.session_store.read_events(
                after_seq=covered, scope="session", event_type="message"
            )
            if (view := project_session_message(event)) is not None
        ]

    def _compaction_selection(self, token_budget: int, token_counter):
        state = self.session_store.read_state()
        covered = max(state.get("covered_through_seq", 0), state.get("cleared_through_seq", 0))
        events = self._main_events(state)
        summary_tokens = (
            token_counter({"content": state.get("summary", "")}) if state.get("summary") else 0
        )
        cutoff = select_safe_prefix(
            events, token_budget - summary_tokens, token_counter=token_counter
        )
        cuts = [
            seq
            for seq in complete_prefix_boundaries(events)
            if cutoff is not None and seq >= cutoff
        ]
        return state, events, cuts

    def _save_compaction(self, state, cutoff, summary):
        from context.sqlite_storage import StateConflictError

        try:
            updated = self.session_store.update_state(
                {"summary": summary.strip(), "covered_through_seq": cutoff},
                remove_keys=("checkpoint",),
                expected_revision=state["state_revision"],
            )
        except StateConflictError:
            return self.session_store.read_state()
        self.short_term.clear()
        for event in self._main_events(updated):
            self.short_term.add_event(event)
        return updated

    @staticmethod
    def _summary_fits(summary, events, cutoff, token_budget, token_counter):
        return bool(summary.strip()) and (
            token_counter({"content": summary})
            + sum(token_counter(e) for e in events if e["seq"] > cutoff)
            <= token_budget
        )

    def compact_if_needed(self, token_budget: int, *, summarizer, token_counter=estimate_tokens):
        """Only commit a complete prefix when its replacement fits the budget."""
        state, events, cuts = self._compaction_selection(token_budget, token_counter)
        for cutoff in cuts:
            selected = [e for e in events if e["seq"] <= cutoff]
            try:
                summary = summarizer(state.get("summary", ""), selected)
                if self._summary_fits(summary, events, cutoff, token_budget, token_counter):
                    return self._save_compaction(state, cutoff, summary)
            except (ContextBudgetExceeded, ModelCallLimitExceeded):
                raise
            except Exception:
                logger.exception("Could not compact session context; raw events remain available")
                return state
        return state

    async def compact_if_needed_async(self, token_budget: int, *, token_counter=estimate_tokens):
        state, events, cuts = self._compaction_selection(token_budget, token_counter)
        if self.llm_model is None:
            return state
        for cutoff in cuts:
            selected = [e for e in events if e["seq"] <= cutoff]
            prompt = (
                "概括以下完整已完成用户轮次。保留用户目标、硬约束、已确认事实和待处理事项。"
                "不要添加原文没有的事实。只返回简洁摘要。\n"
                f"已有摘要：{state.get('summary', '')}\n"
                f"待压缩主消息：{json.dumps(selected, ensure_ascii=False)}"
            )
            try:
                from context.telemetry import model_stage

                with budget_scope("summary"), model_stage("main:summary"):
                    response = await self.llm_model([{"role": "user", "content": prompt}])
                    summary = await _read_model_text(response)
                if self._summary_fits(summary, events, cutoff, token_budget, token_counter):
                    return self._save_compaction(state, cutoff, summary)
            except (ContextBudgetExceeded, ModelCallLimitExceeded):
                raise
            except Exception:
                logger.exception("Could not compact session context; raw events remain available")
                return state
        return state

    def get_compacted_context(self, n_turns: int | None = None) -> Dict[str, Any]:
        state = self.session_store.read_state()
        covered = max(state.get("covered_through_seq", 0), state.get("cleared_through_seq", 0))
        events = self._main_events(state)
        recent = ShortTermMemory()
        for event in events:
            projected = project_session_message(event)
            if projected is not None:
                recent.add_event(projected)
        summary = state.get("summary", "")
        checkpoint = state.get("checkpoint", {})
        if checkpoint.get("goal"):
            summary = f"【当前任务目标】{checkpoint['goal']}\n{summary}".strip()
        return {
            "summary": summary,
            "recent_messages": recent.get_recent_context(n_turns),
            "pending_tool_calls": self.get_pending_tool_calls(),
        }

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
                "recent_dialogue": self.get_compacted_context()["recent_messages"],
                "context_string": self.short_term.get_context_string(None),
                "statistics": self.short_term.get_statistics(),
            },
            "long_term": {
                "preferences": self.long_term.get_preference(),
                "chat_history": self.long_term.get_chat_history(10),
                "trip_history": self.long_term.get_trip_history(5),
                "frequent_destinations": self.long_term.get_frequent_destinations(3),
                "statistics": self.long_term.get_statistics(),
            },
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
        context_str = self.short_term.get_context_string(None)
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
        from context.sqlite_storage import StateConflictError

        candidates = []
        for session in SessionStore.list_sessions(
            self.long_term.storage_path, self.user_id, limit=None
        ):
            session_id = session["session_id"]
            if session_id == self.session_id:
                continue
            store = SessionStore(self.long_term.storage_path, self.user_id, session_id)
            state = store.read_state()
            last_seq = store.latest_message_seq()
            cleared = state.get("cleared_through_seq", 0)
            if last_seq <= cleared:
                continue
            summary = state.get("session_summary", "")
            through = max(cleared, state.get("session_summary_through_seq", 0))
            if not summary or through < last_seq:
                newer = store.read_events(after_seq=through, scope="session", event_type="message")
                lines = [
                    f"{event['role']}: {event.get('content', '')}"
                    for raw in newer
                    if (event := project_session_message(raw)) is not None
                ]
                if self.llm_model:
                    prompt = (
                        "更新这段历史会话的简短摘要。保留用户偏好、已确认事实和重要约束，不要添加没有依据的信息。\n"
                        f"旧摘要：{summary}\n新增消息：{'；'.join(lines)}\n摘要："
                    )
                    try:
                        with budget_scope("summary"):
                            response = await self.llm_model([{"role": "user", "content": prompt}])
                            summary = (await _read_model_text(response)).strip()
                    except (ContextBudgetExceeded, ModelCallLimitExceeded):
                        raise
                    except Exception:
                        logger.exception("Could not summarize prior session %s", session_id)
                if not summary:
                    summary = "；".join(lines[-8:])[-800:]
                try:
                    store.update_state(
                        {"session_summary": summary, "session_summary_through_seq": last_seq},
                        expected_revision=state["state_revision"],
                    )
                except StateConflictError:
                    continue
            candidates.append((session_id, summary, session["updated_at"]))

        if not candidates:
            return ""
        normalized_query = "".join(character.lower() for character in query if character.isalnum())
        grams = {
            normalized_query[index : index + 2]
            for index in range(max(0, len(normalized_query) - 1))
        }

        def score(item):
            normalized_summary = "".join(
                character.lower() for character in item[1] if character.isalnum()
            )
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
            logger.warning(
                "get_long_term_summary called from async context, please use get_long_term_summary_async instead"
            )
            return ""
        except RuntimeError:
            # 没有运行的事件循环，可以使用 asyncio.run
            return asyncio.run(self.get_long_term_summary_async(max_messages))
