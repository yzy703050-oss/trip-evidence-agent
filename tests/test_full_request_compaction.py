import json
from types import SimpleNamespace

import pytest

from agents.main_agent import MainAgent
from context.context_budget import BudgetedModel, count_input_tokens
from context.memory_manager import MemoryManager


def populated(tmp_path, turns=6):
    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    for index in range(turns):
        turn = memory.start_turn(f"日期已更正{index}" + "x" * 300)
        memory.record_message("assistant", "房价未知" + "y" * 300, turn, final=True)
    memory.start_turn("当前请求")
    return memory


def test_full_request_callback_overrides_event_sum_and_expands_covered_prefix(tmp_path):
    memory = populated(tmp_path)
    seen = []

    def summarize(previous, events):
        seen.append((previous, events))
        return "保留更正日期、未知房价"

    state = memory.compact_if_needed(
        999999,
        summarizer=summarize,
        request_fits=lambda summary, remaining: bool(summary) and len(remaining) <= 7,
    )
    assert state["covered_through_seq"] > 0
    assert len(seen) > 1
    assert all(set(e) <= {"role", "content", "result_summary"} for _, rows in seen for e in rows)
    # Later calls summarize newly covered turns together with the previous candidate.
    assert seen[1][0] == "保留更正日期、未知房价"
    assert "更正1" in seen[1][1][0]["content"]


def test_failed_candidates_never_advance_coverage_and_calls_are_bounded(tmp_path):
    memory = populated(tmp_path, 10)
    before = memory.session_store.read_state()
    calls = []
    state = memory.compact_if_needed(
        999999,
        summarizer=lambda previous, events: calls.append(events) or "仍太长",
        request_fits=lambda summary, remaining: False,
    )
    assert len(calls) == 3
    assert state == before
    assert memory.session_store.read_state() == before


def test_concurrent_clear_rejects_stale_candidate_summary(tmp_path):
    memory = populated(tmp_path)

    def summarize(previous, events):
        memory.clear_short_term()
        return "旧日期"

    state = memory.compact_if_needed(
        999999, summarizer=summarize, request_fits=lambda summary, remaining: bool(summary)
    )
    assert state["summary"] == ""
    assert state["cleared_through_seq"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [14854, 20000])
@pytest.mark.parametrize("fixed_factor", [2.2, 2.8])
async def test_main_compacts_at_eighty_percent_using_whole_request(tmp_path, budget, fixed_factor):
    memory = populated(tmp_path)
    inputs = []

    async def model(messages, **kwargs):
        inputs.append(messages)
        if "待压缩主消息" in messages[0]["content"]:
            return SimpleNamespace(text="日期更正，房价未知")
        return SimpleNamespace(text="ok")

    wrapped = BudgetedModel(model, budget_tokens=budget)
    memory.llm_model = wrapped
    main = MainAgent(wrapped, memory_manager=memory)

    # History alone is far below its per-event allowance, but escaped nesting
    # and the fixed request put the actual request above the soft trigger.
    def build(view):
        return [
            {
                "role": "user",
                "content": "f" * int(budget * fixed_factor) + json.dumps(view, ensure_ascii=False),
            }
        ]

    before = count_input_tokens(build(main._refresh_history({}))).tokens
    assert before > budget * 0.8
    await main._request(build, context={}, stage="main")
    assert len(inputs) >= 2
    assert memory.session_store.read_state()["covered_through_seq"] > 0
    target = budget * 0.8 if fixed_factor == 2.2 else budget
    assert count_input_tokens(inputs[-1]).tokens <= target


@pytest.mark.asyncio
async def test_output_compaction_reuses_existing_business_response(tmp_path):
    memory = populated(tmp_path)
    business_calls = []

    async def model(messages, **kwargs):
        if "待压缩主消息" in messages[0]["content"]:
            return SimpleNamespace(text="日期更正、房价未知")
        business_calls.append(messages)
        return SimpleNamespace(text="answer" * 800)

    wrapped = BudgetedModel(model, budget_tokens=5000)
    memory.llm_model = wrapped
    main = MainAgent(wrapped, memory_manager=memory)
    result = await main._request(
        lambda view: [
            {"role": "user", "content": "fixed" * 1500 + json.dumps(view, ensure_ascii=False)}
        ],
        context={},
        stage="main",
    )
    assert result.text == "answer" * 800
    assert len(business_calls) == 1
    assert memory.session_store.read_state()["covered_through_seq"] > 0


@pytest.mark.asyncio
async def test_summary_generation_reserves_input_headroom_and_configured_output_cap(tmp_path):
    memory = populated(tmp_path)
    options = []

    async def model(messages, **kwargs):
        options.append((messages, kwargs))
        return SimpleNamespace(text="简短可靠摘要")

    model.generate_kwargs = {"max_tokens": 1116}
    memory.llm_model = BudgetedModel(model, budget_tokens=1800)
    await memory.compact_if_needed_async(300)
    assert options
    for messages, kwargs in options:
        assert 0 < kwargs["max_tokens"] <= 1116
        assert kwargs["max_tokens"] + count_input_tokens(messages).tokens + 32 <= 1800


@pytest.mark.asyncio
async def test_exact_eighty_percent_boundary_attempts_compaction(tmp_path):
    memory = populated(tmp_path)
    seen = []

    async def model(messages, **kwargs):
        seen.append(messages)
        return SimpleNamespace(
            text="简短摘要" if "待压缩主消息" in messages[0]["content"] else "ok"
        )

    wrapped = BudgetedModel(model, budget_tokens=20000)
    memory.llm_model = wrapped
    main = MainAgent(wrapped, memory_manager=memory)
    padding = ""

    def build(view):
        return [{"role": "user", "content": padding + json.dumps(view, ensure_ascii=False)}]

    current = count_input_tokens(build(main._refresh_history({}))).tokens
    padding = "f" * (16000 - current) * 3
    assert count_input_tokens(build(main._refresh_history({}))).tokens == 16000
    await main._request(build, context={}, stage="main")
    assert len(seen) > 1


@pytest.mark.asyncio
async def test_old_15147_boundary_compacts_when_per_event_history_claims_it_fits(tmp_path):
    memory = MemoryManager("old-boundary", "s", storage_path=str(tmp_path))
    for _ in range(4):
        turn = memory.start_turn('说明"更正日期"与路径\\记录' * 80)
        memory.record_message("assistant", '保留"未知房价"与引用\\记录' * 80, turn, final=True)
    memory.start_turn("本轮请求")
    calls = []

    async def model(messages, **kwargs):
        calls.append(messages)
        return SimpleNamespace(
            text="日期已更正，房价未知" if "待压缩主消息" in messages[0]["content"] else "ok"
        )

    wrapped = BudgetedModel(model, budget_tokens=14854)
    memory.llm_model = wrapped
    main = MainAgent(wrapped, memory_manager=memory)
    padding = ""

    def build(view):
        return [{"role": "user", "content": padding + json.dumps(view, ensure_ascii=False)}]

    view = main._refresh_history({})
    initial = count_input_tokens(build(view)).tokens
    padding = "f" * (15147 - initial) * 3
    assert count_input_tokens(build(view)).tokens == 15147
    fixed = count_input_tokens(build(dict(view, session_summary="", recent_messages=[]))).tokens
    events = memory._main_events(memory.session_store.read_state())
    assert sum(count_input_tokens([e]).tokens for e in events) <= 14854 - fixed
    await main._request(build, context={}, stage="main")
    assert len(calls) > 1
    assert count_input_tokens(calls[-1]).tokens < wrapped.compaction_trigger_tokens
