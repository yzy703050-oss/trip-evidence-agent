from copy import deepcopy


def test_checker_detects_shifted_hard_meeting_in_estimate_draft():
    from evals.manual.single_pass_planning import assess

    record = {
        "case": "hard_conflict",
        "activity_date": "2026-10-21",
        "metrics": {"logical_model_calls": 1},
        "result": {"final_answer": "估算草案", "execution_facts": {"draft_saved": True}},
        "trips": [
            {
                "tasks": [
                    {
                        "purpose": "business",
                        "conditions": {"departure_date": "2026-10-21"},
                        "content": [{"date": "2026-10-22", "time": "上午", "strength": "hard"}],
                    }
                ]
            }
        ],
    }
    before = deepcopy(record)
    assert "hard_meeting_changed" in assess(record)["failures"]
    assert record == before
