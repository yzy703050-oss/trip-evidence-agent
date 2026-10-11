import io
from types import SimpleNamespace

import pytest
from rich.console import Console
from workflow_support import populated_workflow

from cli import TripEvidenceCLI
from context.memory_manager import MemoryManager
from context.session_store import SessionStore
from context.telemetry import MeteredModel


def app_for(root):
    app = TripEvidenceCLI()
    app.user_id = "alice"
    app.session_id = "current"
    app.memory_manager = MemoryManager("alice", "current", storage_path=str(root))
    app.console = Console(file=io.StringIO(), width=250, color_system=None)

    async def model(*args, **kwargs):
        return SimpleNamespace(content="回复")

    app.model = MeteredModel(model, app.memory_manager.session_store.append_run)
    app.harness = SimpleNamespace(agent_registry=SimpleNamespace(providers={}))
    return app


def previous(root, user="alice", session="old"):
    m = MemoryManager(user, session, storage_path=str(root))
    m.start_turn("我要去上海玩然后再去北京")
    m.session_store.write_state({"summary": "旧摘要"})
    return m


def test_startup_always_creates_session_without_prompt(tmp_path, monkeypatch):
    previous(tmp_path)
    # An old directory must not reintroduce the old startup prompt either.
    old_path = tmp_path / "alice" / "sessions" / "legacy"
    old_path.mkdir(parents=True)
    (old_path / "events.jsonl").write_text("{}\n", encoding="utf-8")
    app = app_for(tmp_path)

    def unexpected(*args, **kwargs):
        raise AssertionError("启动不应要求选择会话")

    monkeypatch.setattr("cli.Prompt.ask", unexpected)
    first = app.choose_session_id(str(tmp_path))
    assert len(first) == 32 and first != app.choose_session_id(str(tmp_path))
    assert not app.console.file.getvalue()


def test_list_sessions_uses_topics_and_current_user_only(tmp_path):
    previous(tmp_path)
    previous(tmp_path, user="bob", session="foreign")
    rows = SessionStore.list_sessions(tmp_path, "alice")
    assert [row["session_id"] for row in rows] == ["old"]
    assert rows[0]["title"] == "我要去上海玩然后再去北京"
    assert rows[0]["updated_at"]


def test_resume_cancel_keeps_current_binding(tmp_path, monkeypatch):
    previous(tmp_path)
    app = app_for(tmp_path)
    before = app.memory_manager
    monkeypatch.setattr("cli.Prompt.ask", lambda *a, **kw: "")
    assert app.resume_session() is False
    assert app.memory_manager is before and app.session_id == "current"
    text = app.console.file.getvalue()
    assert "我要去上海" in text and "old" not in text


@pytest.mark.asyncio
async def test_resume_rebinds_messages_agents_and_telemetry_without_model_call(
    tmp_path, monkeypatch
):
    prior = previous(tmp_path)
    workflow = prior.workflow_store.save(populated_workflow(), expected_revision=None)
    prior.set_active_workflow(workflow["id"])
    app = app_for(tmp_path)
    answers = iter(["999", "1"])
    monkeypatch.setattr("cli.Prompt.ask", lambda *a, **kw: next(answers))
    assert app.resume_session() is True
    assert app.session_id == "old"
    assert app.memory_manager.session_store.read_state()["summary"] == "旧摘要"
    assert app.memory_manager.get_active_workflows()[0]["id"] == workflow["id"]
    assert app.harness.memory_manager is app.memory_manager
    assert app.harness.agent_registry.memory_manager is app.memory_manager
    assert app._agent_cache == {}
    assert app.memory_manager.session_store.read_runs() == []
    await app.model([])
    assert len(app.memory_manager.session_store.read_runs()) == 1
    assert SessionStore(tmp_path, "alice", "current").read_runs() == []
    app.memory_manager.start_turn("继续")
    assert app.memory_manager.session_store.read_events()[-1]["content"] == "继续"


def test_resume_construction_failure_is_atomic(tmp_path, monkeypatch):
    previous(tmp_path)
    app = app_for(tmp_path)
    before = (app.memory_manager, app.model, app.harness)
    monkeypatch.setattr("cli.Prompt.ask", lambda *a, **kw: "1")

    def fail(*a, **kw):
        raise OSError("读取失败")

    monkeypatch.setattr("cli.MemoryManager", fail)
    assert app.resume_session() is False
    assert (app.memory_manager, app.model, app.harness) == before
    assert app.session_id == "current"


def test_new_session_does_not_get_prior_active_tasks(tmp_path):
    old = previous(tmp_path)
    workflow = populated_workflow()
    workflow["status"] = "partial"
    saved = old.workflow_store.save(workflow, expected_revision=None)
    old.set_active_workflow(saved["id"])
    current = MemoryManager("alice", "new", storage_path=str(tmp_path))
    assert current.get_active_workflows() == []
    assert current.get_workflow_context("我要去上海玩然后再去北京") == []
    assert current.get_workflow_context("继续上次的行程")[0]["id"] == saved["id"]
    assert (
        MemoryManager("bob", "new", storage_path=str(tmp_path)).get_workflow_context(
            "继续上次的行程"
        )
        == []
    )


def test_empty_resume_list_does_not_ask(tmp_path, monkeypatch):
    app = app_for(tmp_path)
    monkeypatch.setattr("cli.Prompt.ask", lambda *a, **kw: pytest.fail("空列表不应询问"))
    assert app.resume_session() is False
    output = app.console.file.getvalue()
    assert '直接开始聊天' in output and '以后' in output


def test_new_conversation_is_available_after_reopening(tmp_path):
    app = app_for(tmp_path)
    app.memory_manager.long_term.set_user_preference('home_location', '重庆')
    app.memory_manager.start_turn('我明天想去上海玩')
    reopened = MemoryManager('alice', 'another', storage_path=str(tmp_path))
    assert reopened.long_term.get_preference('home_location') == '重庆'
    rows = SessionStore.list_sessions(tmp_path, 'alice')
    assert [(r['session_id'], r['title']) for r in rows] == [('current', '我明天想去上海玩')]
    assert SessionStore.list_sessions(tmp_path, 'bob') == []


def test_corrupt_session_is_not_offered_for_restore(tmp_path):
    memory = previous(tmp_path)
    memory.session_store.events_path.write_text("{broken}\n", encoding="utf-8")
    assert SessionStore.list_sessions(tmp_path, "alice") == []


@pytest.mark.asyncio
async def test_plain_new_plan_does_not_inject_prior_origin_into_main(tmp_path):
    from agents.execution_harness import ExecutionHarness

    old = previous(tmp_path)
    old.start_turn("上海出发，10月18日去北京")
    app = app_for(tmp_path)
    observed = []

    class Main:
        async def initialize(self, context):
            observed.append(context)
            return {
                "response_mode": "direct",
                "finalization_mode": "synthesize",
                "agent_schedule": [],
                "final_answer": "这次从哪里出发？",
            }

    app.harness = ExecutionHarness(Main(), {}, app.memory_manager)
    assert await app.process_query("我要去上海玩然后再去北京")
    assert observed[0]["recent_messages"] == []
    assert observed[0]["known_workflows"] == []
    assert "上海出发" not in observed[0]["history_summary"]
    assert old.session_store.read_runs() == []
