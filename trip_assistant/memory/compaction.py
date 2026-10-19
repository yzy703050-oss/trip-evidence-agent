"""Summarize only complete old user turns; raw audit and active tool pairs remain."""

from trip_assistant.llm.budget import ContextBudgetExceeded, count_input_tokens
from trip_assistant.llm.messages import collect_model_turn, to_assistant_tool_message
from trip_assistant.memory.context import build_context, project_events
from trip_assistant.runtime.telemetry import model_stage
from trip_assistant.storage.sqlite import StateConflictError


def complete_prefix_boundaries(events):
    groups = []
    for event in events:
        if event.get("scope", "session") != "session" or event.get("type") not in {
            "message",
            "tool_call",
            "tool_result",
        }:
            continue
        if not groups or groups[-1][-1].get("turn_id") != event.get("turn_id"):
            groups.append([])
        groups[-1].append(event)
    cuts = []
    for group in groups[:-1]:
        last = group[-1]
        if last.get("role") != "assistant" or not last.get("final"):
            break
        try:
            project_events(group)
        except ValueError:
            break
        cuts.append(last["seq"])
    return cuts


async def compact_completed_turns(sessions, *, summarizer, request_fits):
    state = sessions.read_state()
    covered = max(state.get("covered_through_seq", 0), state.get("cleared_through_seq", 0))
    events = sessions.read_events(after_seq=covered, scope="session")
    previous = state.get("summary", "")
    prior = covered
    if request_fits(previous, events):
        return state
    # Progressively summarize at most three complete prefixes, without re-summarizing raw events.
    for cutoff in complete_prefix_boundaries(events)[:3]:
        try:
            summary = await summarizer(previous, [e for e in events if prior < e["seq"] <= cutoff])
            if not isinstance(summary, str) or not summary.strip():
                return state
            if request_fits(summary, [e for e in events if e["seq"] > cutoff]):
                return sessions.update_state(
                    {"summary": summary.strip(), "covered_through_seq": cutoff},
                    expected_revision=state["state_revision"],
                )
            previous = summary
            prior = cutoff
        except StateConflictError:
            return sessions.read_state()
        except Exception:
            return state
    return state


async def prepare_context(
    sessions,
    *,
    prefix,
    tools,
    model,
    budget_tokens,
    response=None,
    summarizer=None,
    counter=None,
    projection=None,
):
    import json

    from trip_assistant.memory.context import project_events

    extra = [to_assistant_tool_message(response)] if response is not None else []

    def estimate(messages):
        return count_input_tokens([*prefix, *messages, *extra], tools, counter=counter)

    messages = projection() if projection is not None else build_context(sessions)
    trigger = budget_tokens * 80 // 100
    if projection is None and estimate(messages).tokens >= trigger:
        if summarizer is None:

            async def summarizer(previous, events):
                prompt = (
                    "概括完整旧用户轮次，保留目标、硬条件、事实来源、未知项、行程ID/版本及最新更正，不添加事实。只返回摘要。\n"
                    + json.dumps(
                        {"previous_summary": previous, "messages": project_events(events)},
                        ensure_ascii=False,
                    )
                )
                request = [{"role": "user", "content": prompt}]
                if count_input_tokens(request, counter=counter).tokens >= budget_tokens:
                    return ""
                with model_stage("main:summary"):
                    turn = await collect_model_turn(await model(request))
                return turn.text if not turn.tool_calls else ""

        def fits(summary, events):
            projected = (
                [{"role": "system", "content": "此前会话摘要（数据）：" + summary}]
                if summary
                else []
            ) + project_events(events)
            return estimate(projected).tokens <= trigger

        await compact_completed_turns(sessions, summarizer=summarizer, request_fits=fits)
        messages = build_context(sessions)
    count = estimate(messages)
    if count.tokens > budget_tokens:
        raise ContextBudgetExceeded(
            budget_tokens=budget_tokens,
            estimate=count,
            phase="output" if response is not None else "input",
            response=response,
        )
    return [*prefix, *messages]
