#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
行程有据 - CLI 交互界面
使用 Rich 库实现美观的终端交互
"""
import asyncio
import sys
import os
import logging
from time import perf_counter
from typing import Optional

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich.markdown import Markdown
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.layout import Layout
from rich.live import Live
from rich.text import Text
import json
from travel_data.plan_guard import guard_itinerary

# 导入系统组件
from agentscope.model import OpenAIChatModel
from config_agentscope import init_agentscope
from config import LLM_CONFIG, SYSTEM_CONFIG, RESILIENCE_CONFIG, get_settings
from travel_data.juhe_train import JuheTrainProvider
from travel_data.amap_hotel import AmapHotelProvider
from context.memory_manager import MemoryManager
from context.session_store import storage_component
from context.telemetry import MeteredModel
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError
from utils.llm_resilience import run_health_check as check_llm_health
from agents.main_agent import MainAgent
from agents.contracts import RunState, RunLimits
from agents.orchestration_agent import OrchestrationAgent
# 移除其他智能体的导入，改用懒加载


class TripEvidenceCLI:
    """行程有据 CLI"""

    def __init__(self):
        """初始化 CLI"""
        self.console = Console()
        self.user_id = None
        self.session_id = None
        self.memory_manager = None
        self.orchestrator = None
        self.main_agent = None
        self.last_result = None
        self.model = None
        self._agent_cache = {}  # 智能体缓存
        self.circuit_breaker = None  # 在 initialize_system 中从 RESILIENCE_CONFIG 初始化

    def print_banner(self):
        """打印欢迎横幅"""
        self.console.print("\n[bold cyan]🌏 行程有据[/bold cyan] - 让差旅更简单\n", style="bold")

    def print_help(self):
        """打印帮助信息"""
        table = Table(title="命令列表", show_header=True, header_style="bold magenta")
        table.add_column("命令", style="cyan", width=20)
        table.add_column("说明", style="white")

        table.add_row("help", "显示此帮助信息")
        table.add_row("status", "查看当前状态和记忆")
        table.add_row("health", "检查 LLM 服务是否可用")
        table.add_row("clear", "清空当前任务（保留长期记忆）")
        table.add_row("history", "查看历史行程")
        table.add_row("preferences", "查看用户偏好")
        table.add_row("preferences set 类型 值", "手动设置偏好（列表可用 JSON 数组）")
        table.add_row("preferences delete 类型", "删除偏好并阻止 Agent 自动恢复")
        table.add_row("exit", "退出程序")
        table.add_row("", "")
        table.add_row("[自然语言]", "直接输入您的需求，如：")
        table.add_row("", "  - 我要从上海去北京出差")
        table.add_row("", "  - 北京的住宿标准是多少")
        table.add_row("", "  - 查询明天的天气")

        self.console.print(table)

    async def initialize_system(self):
        """初始化系统 - 使用懒加载优化启动速度"""
        # 获取用户信息
        self.user_id = Prompt.ask(
            "用户ID",
            default="default_user"
        )

        self.session_id = self.choose_session_id()

        with self.console.status("初始化中...", spinner="dots"):
            # 初始化AgentScope
            init_agentscope()

            # 初始化模型
            timeout_sec = SYSTEM_CONFIG.get("timeout", 60)
            raw_model = OpenAIChatModel(
                model_name=LLM_CONFIG["model_name"],
                api_key=LLM_CONFIG["api_key"] or os.getenv("DEEPSEEK_API_KEY", ""),
                client_kwargs={
                    "base_url": LLM_CONFIG["base_url"],
                    "timeout": float(timeout_sec),
                },
                generate_kwargs={
                    "temperature": LLM_CONFIG.get("temperature", 0.7),
                    "max_tokens": LLM_CONFIG.get("max_tokens", 2000),
                },
            )

            # 初始化记忆管理器（传入LLM模型用于总结）
            self.memory_manager = MemoryManager(
                user_id=self.user_id,
                session_id=self.session_id,
                llm_model=None
            )
            self.model = MeteredModel(
                raw_model,
                self.memory_manager.session_store.append_run,
                input_usd_per_million=LLM_CONFIG["input_usd_per_million"],
                output_usd_per_million=LLM_CONFIG["output_usd_per_million"],
            )
            self.memory_manager.llm_model = self.model

            self.main_agent = MainAgent(model=self.model)

            # 使用懒加载注册器（智能体在首次使用时才加载）
            from agents.lazy_agent_registry import LazyAgentRegistry
            self._agent_cache = {}
            train_key = get_settings().juhe_train_api_key
            providers = {"train_search": JuheTrainProvider(train_key)} if train_key.strip() else {}
            providers['hotel_search'] = AmapHotelProvider(get_settings().amap_api_key)
            lazy_registry = LazyAgentRegistry(
                model=self.model,
                cache=self._agent_cache,
                memory_manager=self.memory_manager,
                providers=providers,
            )

            # 预先加载关键智能体（可选，利用 preload）
            # lazy_registry.preload("memory_query", "preference")

            # 初始化协调器
            self.orchestrator = OrchestrationAgent(
                name="OrchestrationAgent",
                main_agent=self.main_agent,
                agent_registry=lazy_registry,
                memory_manager=self.memory_manager
            )

            # 熔断器（连接与可用性）
            rc = RESILIENCE_CONFIG
            self.circuit_breaker = CircuitBreaker(
                failure_threshold=rc.get("circuit_failure_threshold", 5),
                recovery_timeout_sec=rc.get("circuit_recovery_timeout_sec", 60.0),
                half_open_successes=rc.get("circuit_half_open_successes", 2),
            )

        self.console.print(f"✓ 就绪 (用户: {self.user_id}, 会话: {self.session_id}) - 输入 help 查看帮助\n", style="green")

    def choose_session_id(self, storage_path: str = "data/memory") -> str:
        """An empty answer creates a session; an existing ID resumes it."""
        from pathlib import Path
        import uuid

        storage_component(self.user_id)
        sessions_dir = Path(storage_path) / self.user_id / "sessions"
        known = sorted(
            (path for path in sessions_dir.iterdir() if path.is_dir() and (path / "events.jsonl").exists()),
            key=lambda path: (path / "events.jsonl").stat().st_mtime,
            reverse=True,
        ) if sessions_dir.exists() else []
        if known:
            self.console.print("可恢复的会话: " + ", ".join(path.name for path in known[:5]))
        else:
            return uuid.uuid4().hex
        requested = Prompt.ask("会话ID（回车新建，输入已有ID恢复）", default="").strip()
        if not requested:
            return uuid.uuid4().hex
        storage_component(requested)
        if not (sessions_dir / requested / "events.jsonl").exists():
            raise ValueError(f"找不到会话 {requested}")
        return requested

    def handle_preferences_command(self, raw_command: str) -> None:
        parts = raw_command.strip().split(maxsplit=3)
        if len(parts) == 4 and parts[1].lower() == "set":
            try:
                value = json.loads(parts[3])
            except json.JSONDecodeError:
                value = parts[3]
            self.memory_manager.long_term.set_user_preference(parts[2], value)
            self.console.print(f"✓ 已设置 {parts[2]}", style="green")
        elif len(parts) == 3 and parts[1].lower() == "delete":
            self.memory_manager.long_term.delete_user_preference(parts[2])
            self.console.print(f"✓ 已删除 {parts[2]}", style="green")
        else:
            self.console.print("用法: preferences set 类型 值 / preferences delete 类型", style="yellow")

    async def process_query(self, user_input: str):
        """Measure a whole query even when a stage exits early or fails."""
        started = perf_counter()
        status = "incomplete"
        error = None
        try:
            completed = await self._process_query_impl(user_input)
            if completed:
                status = "completed"
            return completed
        except Exception as exc:
            status = "error"
            error = str(exc)
            raise
        finally:
            if self.memory_manager:
                try:
                    self.memory_manager.session_store.append_run({
                        "type": "query_run", "turn_id": self.memory_manager.current_turn_id,
                        "status": status, "error": error,
                        "finalization_method": (self.last_result or {}).get("finalization_method"),
                        "latency_ms": round((perf_counter() - started) * 1000, 3),
                    })
                except Exception:
                    logging.getLogger(__name__).exception("Could not persist query telemetry")

    async def _process_query_impl(self, user_input: str):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from config import RUN_LIMITS
        turn_id = self.memory_manager.start_turn(user_input)
        if self.circuit_breaker:
            try:
                self.circuit_breaker.raise_if_open()
            except CircuitOpenError:
                self.console.print("服务暂时不可用，请稍后再试。", style="yellow")
                return False
        await self.memory_manager.compact_if_needed_async(
            token_budget=SYSTEM_CONFIG.get('memory_context_budget_tokens', 6000))
        history = await self._get_long_term_summary(user_input)
        snapshot = self.memory_manager.get_compacted_context(n_turns=5)
        context = {'original_query': user_input, 'current_time': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                   'history_summary': history, 'session_summary': snapshot['summary'],
                   'recent_messages': [m for m in snapshot['recent_messages'] if m.get('turn_id') != turn_id]}
        run = RunState(turn_id, limits=RunLimits(**RUN_LIMITS),
                       effective_preferences=self.memory_manager.long_term.get_preference())
        self.current_run = run
        try:
            with self.console.status("思考中...", spinner='dots'):
                result = await self.orchestrator.run_turn(context, run)
            if self.circuit_breaker:
                if result['status'] == 'error': self.circuit_breaker.record_failure()
                else: self.circuit_breaker.record_success()
        except Exception:
            if self.circuit_breaker: self.circuit_breaker.record_failure()
            raise
        self.last_result = result
        self._display_agents_called(result)
        self._display_results(result)
        self.memory_manager.record_message('assistant', json.dumps(result, ensure_ascii=False), turn_id,
            final=True, metadata={'format': 'main_result', 'finalization_method': result['finalization_method']})
        return result['status'] != 'error'

    def _display_agents_called(self, result_data: dict):
        """显示调用的智能体列表"""
        results = result_data.get("results", [])
        if not results:
            return

        # 收集所有调用的智能体
        agents_called = []
        for result in results:
            agent_name = result.get("agent_name", "")
            status = result.get("status", "")

            display_name = self._get_agent_display_name(agent_name)

            # 根据状态添加标记
            if status == "success":
                agents_called.append(f"{display_name} ✓")
            elif status == "error":
                agents_called.append(f"{display_name} ✗")
            else:
                agents_called.append(f"{display_name} ?")

        if agents_called:
            self.console.print()
            self.console.print(f"🤖 调用智能体: {', '.join(agents_called)}", style="dim")

    def _display_results(self, result_data: dict):
        from travel_data.result_guard import guard_domain_result
        from agents.itinerary_module import guard_final_itinerary
        self.console.print()
        if result_data.get('final_answer'):
            self.console.print(result_data['final_answer'], markup=False)
        if result_data.get('missing_fields'):
            self.console.print('需要补充：' + ', '.join(result_data['missing_fields']), markup=False)
        domains = result_data.get('domain_results', {})
        for domain, raw in domains.items():
            data = guard_domain_result(domain, raw)
            if domain in ('train', 'hotel', 'guide'):
                self._display_sourced_result({'train': 'train_search', 'hotel': 'hotel_search', 'guide': 'travel_guide'}[domain], data)
            else:
                source = data.get('source') or {}
                self.console.print(f"{source.get('provider', '')} {source.get('url', '')} {source.get('fetched_at', '')}", markup=False)
                if domain == 'web':
                    for item in data.get('items', []):
                        self.console.print(f"{item.get('title', '')}: {item.get('snippet', '')} {item.get('url', '')}", markup=False)
        if result_data.get('itinerary'):
            plan = guard_final_itinerary(result_data['itinerary'], domains, result_data.get('travel_conditions'))
            self.console.print(json.dumps(plan['itinerary'], ensure_ascii=False, indent=2), markup=False)
            self._display_sourced_plan(plan)
        if result_data.get('status') == 'error':
            self.console.print('本轮未能完成请求。', style='yellow')
        self.console.print()

    async def _get_long_term_summary(self, user_input: str = "") -> str:
        """
        生成长期记忆摘要，用于传递给 MainAgent
        使用LLM总结历史聊天记录 + 结构化偏好

        Args:
            user_input: 用户输入，用于筛选相关历史行程

        Returns:
            格式化的长期记忆摘要
        """
        summary_parts = []

        # 1. 用户偏好信息（始终加载）
        prefs = self.memory_manager.long_term.get_preference()
        if prefs:
            pref_lines = ["【用户背景信息】（来自长期记忆，可用于推断缺失信息）"]

            # 遍历所有偏好，全部加载
            for pref_key, pref_value in prefs.items():
                if pref_value:  # 只添加有值的偏好
                    # 如果是列表，用逗号连接
                    if isinstance(pref_value, list):
                        pref_lines.append(f"• {pref_key}: {', '.join(pref_value)}")
                    else:
                        pref_lines.append(f"• {pref_key}: {pref_value}")

            # 只有在有具体偏好内容时才添加
            if len(pref_lines) > 1:
                summary_parts.extend(pref_lines)

        # 2. 使用LLM总结历史聊天记录
        chat_summary = await self.memory_manager.get_long_term_summary_async(max_messages=50, query=user_input)
        if chat_summary:
            summary_parts.append("\n【历史会话总结】")
            summary_parts.append(chat_summary)

        # 3. 智能筛选相关历史行程
        all_trips = self.memory_manager.long_term.get_trip_history(limit=None)
        if all_trips:
            # 筛选相关的行程（地点匹配）
            relevant_trips = []
            other_trips = []

            for trip in all_trips:
                origin = trip.get("origin", "") or ""
                destination = trip.get("destination", "") or ""

                # 如果用户输入提到了这个行程的地点，标记为相关
                if (origin and origin in user_input) or (destination and destination in user_input):
                    relevant_trips.append(trip)
                else:
                    other_trips.append(trip)

            # 优先显示相关的，再补充最近的
            trips_to_show = relevant_trips[:2] + other_trips[:1]  # 2条相关 + 1条最近

            if trips_to_show:
                summary_parts.append("\n【历史行程】")
                for i, trip in enumerate(trips_to_show[:3], 1):
                    origin = trip.get("origin", "未知")
                    destination = trip.get("destination", "未知")
                    start_date = trip.get("start_date", "")
                    purpose = trip.get("purpose", "")

                    # 标记相关性
                    relevance_mark = "✦ " if trip in relevant_trips else ""
                    summary_parts.append(
                        f"{i}. {relevance_mark}{origin} → {destination} ({start_date}) - {purpose}"
                    )

        return "\n".join(summary_parts) if summary_parts else ""

    def _display_sourced_result(self, name: str, data: dict):
        status = data.get("status", "error")
        labels = {"ok": "查询完成", "partial": "部分完成", "needs_input": "需补充条件",
                  "unavailable": "数据服务不可用", "error": "查询失败"}
        self.console.print(f"{self._get_agent_display_name(name)}: {labels.get(status, '未知状态')} ({status})", markup=False)
        for key, label in (("message", "说明"), ("missing_fields", "缺失条件")):
            if data.get(key):
                self.console.print(f"{label}: {data[key]}", markup=False)
        source = data.get("source") or {}
        self.console.print(f"来源: {source.get('provider', '未提供')} {source.get('url') or ''} 查询时间: {data.get('fetched_at') or source.get('fetched_at') or '未知'}", markup=False)
        rows = [{"agent_name": name, "data": data}]
        if status in {"ok", "partial"}:
            if not data.get("items"):
                self.console.print("查询完成，未返回候选项；无法确认库存。", markup=False)
            for item in data.get("items", []):
                if not isinstance(item, dict):
                    continue
                kind = "train" if name == "train_search" else "hotel"
                checked = guard_itinerary({f"selected_{kind}_id": item.get("id")}, rows)
                offer = checked.get(f"selected_{kind}") if name != "travel_guide" else None
                if offer:
                    self.console.print(json.dumps(offer, ensure_ascii=False), markup=False)
            if name == "travel_guide":
                for fact in guard_itinerary({}, rows)["guide_facts"]:
                    self.console.print(json.dumps(fact, ensure_ascii=False), markup=False)

    def _display_sourced_plan(self, data: dict):
        for kind in ("train", "hotel"):
            if data.get(f"selected_{kind}"):
                self.console.print(json.dumps(data[f"selected_{kind}"], ensure_ascii=False), markup=False)
        for fact in data.get("guide_facts", []):
            self.console.print(json.dumps(fact, ensure_ascii=False), markup=False)
        budget = data["budget"]
        self.console.print(f"已知费用小计: {budget['known_subtotal_cny']} CNY", markup=False)
        if budget["missing_categories"]:
            self.console.print(f"未报价类别: {', '.join(budget['missing_categories'])}；完整预算尚未核实。", markup=False)

    def _get_agent_display_name(self, agent_name: str) -> str:
        """获取智能体的显示名称"""
        # 与 README / LazyAgentRegistry 保持一致，显示业务角色和内部工具
        agent_display_names = {
            "preference": "偏好管理",
            "information_query": "信息查询",
            "rag_knowledge": "知识库查询",
            "memory_query": "记忆查询",
            "train_search": "火车查询",
            "hotel_search": "酒店查询",
            "travel_guide": "旅游攻略",
        }
        return agent_display_names.get(agent_name, agent_name)

    def show_status(self):
        """显示当前状态"""
        # 记忆统计
        full_context = self.memory_manager.get_full_context()
        short_term_stats = full_context["short_term"]["statistics"]
        long_term_stats = full_context["long_term"]["statistics"]

        memory_table = Table(title="记忆状态", show_header=True, header_style="bold magenta")
        memory_table.add_column("类型", style="cyan")
        memory_table.add_column("状态", style="white")

        memory_table.add_row(
            "短期记忆",
            f"{short_term_stats['total_messages']} 条消息"
        )
        memory_table.add_row(
            "长期记忆",
            f"{long_term_stats['total_trips']} 次行程"
        )
        memory_table.add_row(
            "已加载智能体",
            f"{len(self._agent_cache)} 个"
        )

        self.console.print(memory_table)
        self.console.print()

        # 历史对话
        recent_messages = self.memory_manager.short_term.get_recent_context(n_turns=5)
        if recent_messages:
            dialogue_table = Table(title="最近对话 (最多5轮)", show_header=True, header_style="bold cyan")
            dialogue_table.add_column("角色", style="cyan", width=8)
            dialogue_table.add_column("内容", style="white", width=60)
            dialogue_table.add_column("时间", style="dim", width=12)

            for msg in recent_messages:
                role_name = "👤 用户" if msg["role"] == "user" else "🤖 助手"
                content = msg["content"]

                # 截断过长的内容
                if len(content) > 100:
                    content = content[:100] + "..."

                # 格式化时间
                timestamp = msg.get("timestamp", "")
                if timestamp:
                    from datetime import datetime
                    try:
                        dt = datetime.fromisoformat(timestamp)
                        time_str = dt.strftime("%H:%M:%S")
                    except:
                        time_str = ""
                else:
                    time_str = ""

                dialogue_table.add_row(role_name, content, time_str)

            self.console.print(dialogue_table)
            self.console.print()

    async def run_health_check(self):
        """在会话内执行健康检查并显示熔断器状态"""
        if self.circuit_breaker:
            status = self.circuit_breaker.get_status()
            self.console.print(f"[bold]熔断器[/bold]: {status['state']}", style="cyan")
        ok, msg = await check_llm_health(
            base_url=LLM_CONFIG["base_url"],
            api_key=LLM_CONFIG["api_key"],
            model_name=LLM_CONFIG["model_name"],
            timeout_sec=RESILIENCE_CONFIG.get("health_check_timeout_sec", 10.0),
        )
        if ok:
            self.console.print("LLM 服务: [green]正常[/green]", style="bold")
        else:
            self.console.print(f"LLM 服务: [red]不可用[/red] - {msg}", style="bold")
        self.console.print()

    def show_history(self):
        """显示历史行程"""
        history = self.memory_manager.long_term.get_trip_history(10)

        if not history:
            self.console.print("暂无历史行程", style="yellow")
            return

        table = Table(title="历史行程", show_header=True, header_style="bold magenta")
        table.add_column("ID", style="cyan")
        table.add_column("出发地", style="white")
        table.add_column("目的地", style="white")
        table.add_column("日期", style="white")
        table.add_column("目的", style="white")

        for trip in history:
            table.add_row(
                trip.get("trip_id", ""),
                trip.get("origin", ""),
                trip.get("destination", ""),
                trip.get("start_date", ""),
                trip.get("purpose", "")
            )

        self.console.print(table)

    def show_preferences(self):
        """显示用户偏好"""
        prefs = self.memory_manager.long_term.get_preference()

        table = Table(title="用户偏好", show_header=True, header_style="bold magenta")
        table.add_column("类型", style="cyan")
        table.add_column("值", style="white")

        for key, value in prefs.items():
            if value:
                table.add_row(key, str(value))

        self.console.print(table)

    async def run(self):
        """运行 CLI"""
        # 打印横幅
        self.print_banner()

        # 初始化系统
        await self.initialize_system()

        # 主循环
        while True:
            try:
                # 获取用户输入
                user_input = Prompt.ask("\n[cyan]>[/cyan]")

                if not user_input.strip():
                    continue

                # 处理命令
                command = user_input.strip().lower()

                if command == "exit":
                    self.memory_manager.end_session()
                    self.console.print("再见！", style="cyan")
                    break
                elif command == "help":
                    self.print_help()
                elif command == "status":
                    self.show_status()
                elif command == "health":
                    await self.run_health_check()
                elif command == "clear":
                    self.memory_manager.clear_short_term()
                    self.console.print("✓ 已清空短期记忆", style="green")
                elif command == "history":
                    self.show_history()
                elif command == "preferences":
                    self.show_preferences()
                elif command.startswith("preferences "):
                    self.handle_preferences_command(user_input)
                else:
                    # 处理自然语言查询
                    await self.process_query(user_input)

            except KeyboardInterrupt:
                self.console.print("\n使用 'exit' 退出", style="dim")
            except CircuitOpenError:
                self.console.print("\n[bold yellow]⚠ 服务暂时不可用，请稍后再试。[/bold yellow]", style="dim")
            except Exception as e:
                self.console.print(f"\n错误: {e}", style="red")


def run_health_check_standalone() -> int:
    """
    独立执行健康检查（用于 `python cli.py health`）。
    不进入交互式 CLI，只检测 LLM 是否可达。
    Returns:
        0 成功，1 失败（便于脚本/监控）
    """
    import asyncio
    init_agentscope()
    ok, msg = asyncio.run(check_llm_health(
        base_url=LLM_CONFIG["base_url"],
        api_key=LLM_CONFIG["api_key"],
        model_name=LLM_CONFIG["model_name"],
        timeout_sec=RESILIENCE_CONFIG.get("health_check_timeout_sec", 10.0),
    ))
    if ok:
        print("OK")
        return 0
    print(f"FAIL: {msg}")
    return 1


def main():
    """主函数"""
    if len(sys.argv) > 1 and sys.argv[1].strip().lower() == "health":
        exit(run_health_check_standalone())
    cli = TripEvidenceCLI()
    asyncio.run(cli.run())


if __name__ == "__main__":
    main()
