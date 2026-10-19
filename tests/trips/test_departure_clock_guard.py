from copy import deepcopy

import pytest
from support.native import NativeRuntimeFixture, write_then_query
from test_activities import context, plan, select_args

from evals.simulated_travel import SimulatedTrainProvider
from trip_assistant.runtime.errors import ToolFailure


@pytest.mark.asyncio
async def test_new_trip_cannot_depart_before_trusted_current_date(tmp_path):
    train = SimulatedTrainProvider(departure_hours=[7])
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    turn = context(runtime, current_time="2026-10-18T05:00:00+08:00")
    with pytest.raises(ToolFailure) as caught:
        await write_then_query(runtime.trip_service, plan(), turn)
    assert caught.value.code == "DEPARTURE_IN_PAST"
    assert not train.calls and runtime.trip_service.store.list_summaries() == []


@pytest.mark.asyncio
async def test_same_day_departed_candidate_cannot_replace_committed_version(tmp_path):
    runtime = NativeRuntimeFixture(
        tmp_path,
        {
            "train": SimulatedTrainProvider(duration_minutes=120, departure_hours=[7]),
        },
    )
    turn = context(runtime, current_time="2026-10-17T06:00:00+08:00")
    written = await write_then_query(runtime.trip_service, plan(), turn)
    turn["operation_id"] = "t:select"
    chosen = await write_then_query(runtime.trip_service, select_args(written), turn)
    saved = deepcopy(runtime.trip_service.store.load(chosen["trip_plan_id"]))
    turn.update(operation_id="t:late-select", current_time="2026-10-17T08:00:00+08:00")
    late_args = select_args(written)
    late_args["expected_revision"] = chosen["revision"]
    with pytest.raises(ToolFailure) as caught:
        await write_then_query(runtime.trip_service, late_args, turn)
    assert caught.value.code == "DEPARTURE_IN_PAST"
    assert (
        runtime.trip_service.store.load(chosen["trip_plan_id"])["committed"] == saved["committed"]
    )
