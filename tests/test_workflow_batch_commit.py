import pytest
from workflow_support import populated_workflow


def test_batch_version_and_reference_guard():
    from agents.workflow_batches import check_batch_submission

    w = populated_workflow()
    action = {
        "expected_workflow_revision": w["revision"],
        "draft_tasks": [
            {"task_id": t["id"], "task_revision": t["revision"], "draft_plan": t["draft_plan"]}
            for t in w["tasks"]
        ],
    }
    result = check_batch_submission(w, action)
    assert result["validation"]["valid"]
    action["expected_workflow_revision"] += 1
    with pytest.raises(ValueError):
        check_batch_submission(w, action)
