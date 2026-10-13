from workflow_support import populated_workflow

from agents.workflow_guard import check_workflow


def test_amendment_cannot_relax_saved_hard_train_policy():
    w = populated_workflow()
    w["policy_context"] = {
        "status": "ok",
        "missing_fields": [],
        "conflicts": [],
        "references": [{"id": "doc"}],
        "constraints": [
            {
                "id": "train-cap",
                "hard": True,
                "field": "train_max_total_cny",
                "operator": "<=",
                "unit": "CNY",
                "value": 50,
                "evidence_ids": ["doc"],
                "applicability": {},
            }
        ],
    }
    assert any(i["code"] == "policy_constraint_failed" for i in check_workflow(w)["issues"])


def test_unpriced_place_cannot_prove_hard_hotel_cap():
    w = populated_workflow()
    w["policy_context"] = {
        "status": "ok",
        "missing_fields": [],
        "conflicts": [],
        "references": [{"id": "doc"}],
        "constraints": [
            {
                "id": "hotel-cap",
                "hard": True,
                "field": "hotel_max_nightly_cny",
                "operator": "<=",
                "unit": "CNY/night",
                "value": 500,
                "evidence_ids": ["doc"],
                "applicability": {},
            }
        ],
    }
    for row in w["results_by_query"].values():
        if row["domain"] == "hotel":
            for item in row["items"]:
                item.update(kind="hotel_place", city="北京", address="车站旁", location="116,39")
    assert any(i["code"] == "policy_constraint_unverified" for i in check_workflow(w)["issues"])
