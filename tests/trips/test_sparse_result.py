import pytest
from support.workflow import populated_workflow


def test_unbound_query_reference_is_not_reported_as_expired():
    from trip_assistant.trips.service import TripService

    work = populated_workflow(quoted=True)
    ref = {"query_id": "independent", "result_revision": 1, "candidate_id": "c"}
    turn = {"shown_candidates": {"c": ref}}
    with pytest.raises(ValueError, match="task-bound query_id"):
        TripService(None, None, None)._select(work, work["tasks"][0], "train", ref, turn)


def test_completed_trip_does_not_repeat_candidate_pool_in_final_observation():
    from trip_assistant.trips.service import TripService

    work = populated_workflow(quoted=True)
    work["status"] = "completed"
    work["last_candidate_views"] = [{"items": [{"id": "candidate"}]}]
    result = TripService(None, None, None)._result(work)
    assert result["candidates"] == []
    assert result["tasks"] and result["validation"] == work["validation"]


def test_negated_hotel_inventory_is_not_an_inventory_guarantee():
    from trip_assistant.presentation.facts import finalize

    text = "住宿只有地点参考，没有房价和空房信息。"
    result = finalize(
        text,
        [
            {
                "name": "query",
                "status": "ok",
                "data": [
                    {
                        "domain": "hotel",
                        "items": [{"hotel_name": "参考酒店", "availability": "unknown"}],
                    }
                ],
            }
        ],
    )
    assert result["method"] == "main_agent"
