from trip_assistant.presentation.facts import finalize


def test_request_clock_remains_input_data_and_is_not_parsed_from_reply():
    answer = "明天去上海的建议已整理，尚未预订。"
    context = {"current_time": "2026-10-16T10:00:00+08:00", "original_query": "规划明天去上海"}
    checked = finalize(answer, [{"name": "read", "status": "ok", "data": {}}], context=context)
    assert checked["answer"] == answer and checked["execution_facts"]["saved"] is False
    assert context["current_time"] == "2026-10-16T10:00:00+08:00"
