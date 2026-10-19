import pytest
from support.workflow import populated_workflow

from trip_assistant.trips.evidence import query_views, resolve_selection


def test_rejected_candidates_are_removed_from_view_and_cannot_be_selected():
    w = populated_workflow()
    t = w["tasks"][0]
    row = w["results_by_query"][t["query_ids"][0]]
    bad = row["items"][0]["id"]
    t["rejected_candidates"] = {"train": [bad]}
    assert bad not in [i["id"] for r in query_views(w, t["id"]) for i in r["items"]]
    with pytest.raises(ValueError):
        resolve_selection(
            w, t["id"], t["revision"], dict(query_id=row["id"], result_revision=1, candidate_id=bad)
        )
