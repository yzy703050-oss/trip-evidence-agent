"""Rich CLI over the native application; explicit UI commands stay local."""

import asyncio
import json
import logging
import sys
from sqlite3 import Error as SQLiteError
from uuid import uuid4

from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from trip_assistant.bootstrap import create_application
from trip_assistant.llm.client import OpenAIChatModel
from trip_assistant.llm.messages import collect_model_turn
from trip_assistant.runtime.logging import configure_runtime_logging
from trip_assistant.runtime.progress import progress_scope
from trip_assistant.settings import generation_options, get_settings
from trip_assistant.storage.preferences import LongTermMemory
from trip_assistant.storage.sessions import SessionStore, storage_component


class TripEvidenceCLI:
    def __init__(
        self, *, settings=None, storage_root="data/memory", model=None, providers=None, console=None
    ):
        self.settings = settings or get_settings()
        self.storage_root = storage_root
        self.raw_model = model
        self.providers = providers
        self.console = console or Console()
        self.user_id = None
        self.session_id = None
        self.app = None
        self.last_result = None

    def print_banner(self):
        self.console.print("行程有据 — 差旅助手", style="bold cyan")

    def print_help(self):
        table = Table(title="命令列表")
        table.add_column("命令")
        table.add_column("说明")
        for command, description in [
            ("help", "显示帮助"),
            ("status", "查看会话状态"),
            ("health", "检查模型服务"),
            ("clear", "清空当前上下文，保留长期记忆和行程"),
            ("history", "查看行程摘要"),
            ("resume", "选择历史对话，可取消"),
            ("preferences", "查看偏好"),
            ("preferences set 类型 值", "设置明确偏好，支持 JSON 数组"),
            ("preferences delete 类型", "删除偏好并保护撤回"),
            ("exit", "退出"),
        ]:
            table.add_row(command, description)
        self.console.print(table)

    def choose_session_id(self, storage_path=None):
        storage_component(self.user_id)
        return uuid4().hex

    def ensure_home_location(self, memory=None):
        memory = memory or self.app.preferences
        value = memory.get_preference("home_location")
        if isinstance(value, str) and value.strip():
            return value.strip()
        while True:
            city = Prompt.ask("你通常长期住在哪座城市？（必须填写）", console=self.console).strip()
            if city:
                memory.set_user_preference("home_location", city)
                self.console.print(
                    "已记住你的长期居住城市是" + city + "，以后可以修改。", markup=False
                )
                return city
            self.console.print("长期居住城市必须填写。")

    async def initialize_system(self):
        self.user_id = Prompt.ask("用户ID", default="default_user", console=self.console).strip()
        storage_component(self.user_id)
        self.ensure_home_location(LongTermMemory(self.user_id, str(self.storage_root)))
        self._bind_session(self.choose_session_id())
        self.console.print("已开始新对话；输入 help 查看帮助，resume 恢复历史。")

    def _bind_session(self, session_id):
        # Complete construction and recovery checks before replacing any visible binding.
        application = create_application(
            self.settings,
            model=self.raw_model,
            providers=self.providers,
            storage_root=self.storage_root,
            user_id=self.user_id,
            session_id=session_id,
            shared_scheduler=self.app.scheduler
            if self.app and self.app.sessions.user_id == self.user_id
            else None,
        )
        state = application.sessions.read_state()
        active = state.get("active_turn", {})
        if active and active.get("status") not in {"completed", "stopped"}:
            application.restore(active["turn_id"])
        self.app = application
        self.session_id = session_id
        self.raw_model = application.raw_model
        self.providers = application.providers
        self.last_result = None

    def resume_session(self):
        try:
            rows = SessionStore.list_sessions(self.storage_root, self.user_id)
            if not rows:
                self.console.print(
                    "当前用户还没有可恢复的历史对话。可以直接开始聊天，内容会自动保存，"
                    "以后输入 resume 即可继续。"
                )
                return False
            table = Table(title="历史对话")
            for label in ("编号", "对话主题", "最近时间"):
                table.add_column(label)
            for i, row in enumerate(rows, 1):
                table.add_row(str(i), Text(row["title"]), row["updated_at"])
            self.console.print(table)
            while True:
                answer = Prompt.ask(
                    "选择编号（回车取消）", default="", console=self.console
                ).strip()
                if not answer:
                    return False
                if answer.isascii() and answer.isdigit() and 1 <= int(answer) <= len(rows):
                    row = rows[int(answer) - 1]
                    self._bind_session(row["session_id"])
                    self.console.print("已恢复：" + row["title"], markup=False)
                    self.console.print("上次聊到：" + row["last_user_message"], markup=False)
                    return True
                self.console.print("请输入列表中的编号，或回车取消。")
        except (OSError, ValueError, KeyError, SQLiteError):
            self.console.print("这段对话暂时无法恢复，当前对话保持不变。")
            return False

    def show_preferences(self):
        self.console.print(
            json.dumps(
                {
                    "preferences": self.app.preferences.get_preference(),
                    "memories": self.app.preferences.data.get("travel_memories", []),
                },
                ensure_ascii=False,
                indent=2,
            ),
            markup=False,
        )

    def handle_preferences_command(self, command):
        parts = command.split(maxsplit=3)
        if len(parts) == 4 and parts[1].lower() == "set":
            try:
                value = json.loads(parts[3])
            except ValueError:
                value = parts[3]
            self.app.preferences.set_user_preference(parts[2], value)
            self.console.print("已设置 " + parts[2], markup=False)
        elif len(parts) == 3 and parts[1].lower() == "delete":
            self.app.preferences.delete_user_preference(parts[2])
            self.console.print("已删除 " + parts[2], markup=False)
        else:
            self.console.print("用法：preferences set 类型 值 / preferences delete 类型")

    def show_history(self):
        rows = self.app.trips.discovery_summaries()
        if not rows:
            self.console.print("暂无历史行程")
            return
        table = Table(title="行程摘要")
        for label in ("ID", "摘要", "状态", "日期"):
            table.add_column(label)
        for row in rows[:10]:
            table.add_row(
                row["id"],
                row.get("summary", ""),
                row.get("status", ""),
                str(row.get("confirmed_conditions", {})),
            )
        self.console.print(table)

    def show_status(self):
        state = self.app.sessions.read_state()
        self.console.print(
            json.dumps(
                {
                    "session_id": self.session_id,
                    "summary": state.get("summary", ""),
                    "active_turn": state.get("active_turn", {}),
                    "statistics": self.app.preferences.get_statistics(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            markup=False,
        )

    async def process_query(self, query):
        with (
            self.console.status("正在理解请求…", spinner="dots") as status,
            progress_scope(lambda text: status.update(text) if status is not None else None),
        ):
            self.last_result = await self.app.run(query)
        self.console.print(self.last_result["final_answer"], markup=False)
        return self.last_result["status"] != "error"

    async def run_health_check(self):
        ok = await health(self.settings, model=self.raw_model)
        self.console.print("LLM 服务：正常" if ok else "LLM 服务：不可用")
        return ok

    async def handle_command(self, raw):
        command = raw.strip().lower()
        if command == "help":
            self.print_help()
        elif command == "preferences":
            self.show_preferences()
        elif command.startswith("preferences "):
            self.handle_preferences_command(raw)
        elif command == "history":
            self.show_history()
        elif command == "status":
            self.show_status()
        elif command == "resume":
            self.resume_session()
        elif command == "health":
            await self.run_health_check()
        elif command == "clear":
            self.app.sessions.clear_context()
            self.console.print("已清空当前上下文，长期记忆与行程保留。")
        elif command == "exit":
            return True
        else:
            return False
        return True

    async def run(self):
        self.print_banner()
        await self.initialize_system()
        while True:
            try:
                query = Prompt.ask(">", console=self.console).strip()
                if not query:
                    continue
                if query.lower() == "exit":
                    self.console.print("再见！")
                    break
                if not await self.handle_command(query):
                    await self.process_query(query)
            except KeyboardInterrupt:
                self.console.print("使用 exit 退出。")
            except EOFError:
                break
            except Exception:
                logging.getLogger(__name__).exception("CLI request failed")
                self.console.print("处理失败，已保留已保存的数据。", style="red")


async def health(settings, *, model=None):
    try:
        model = model or OpenAIChatModel(
            settings.llm_model,
            api_key=settings.llm_api_key or settings.deepseek_api_key,
            client_kwargs={"base_url": settings.llm_base_url, "timeout": 10},
            generate_kwargs=generation_options(settings.llm_model, max_tokens=5),
        )
        async with asyncio.timeout(10):
            await collect_model_turn(await model([{"role": "user", "content": "1"}], max_tokens=5))
        return True
    except Exception:
        return False


def main():
    if len(sys.argv) > 1 and sys.argv[1] in {"--help", "-h", "help"}:
        TripEvidenceCLI().print_help()
        return
    configure_runtime_logging()
    if len(sys.argv) > 1 and sys.argv[1] == "health":
        ok = asyncio.run(health(get_settings()))
        print("OK" if ok else "FAIL")
        raise SystemExit(0 if ok else 1)
    asyncio.run(TripEvidenceCLI().run())
