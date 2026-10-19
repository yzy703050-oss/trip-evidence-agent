import pytest
from support.native import FakeClock


def test_default_eight_rounds_preserve_remaining_allowance_across_resume():
    from trip_assistant.runtime.limits import LimitError, TurnBudget
    from trip_assistant.settings import Settings

    clock = FakeClock()
    budget = TurnBudget(Settings(_env_file=None), clock=clock)
    for _ in range(5):
        budget.reserve_batch([{}], [], True)
    clock.advance(125)
    restored = TurnBudget(Settings(_env_file=None), budget.checkpoint(), clock=FakeClock(9000))
    assert restored.remaining_seconds() == 475
    for _ in range(3):
        restored.reserve_batch([{}], [], True)
    with pytest.raises(LimitError, match="planning_round_limit_exceeded"):
        restored.reserve_batch([{}], [], True)
    assert restored.rounds == 8


def test_limits_are_preflight_and_resume_does_not_reset():
    from trip_assistant.runtime.limits import LimitError, TurnBudget
    from trip_assistant.settings import Settings

    clock = FakeClock()
    b = TurnBudget(Settings(_env_file=None, planning_rounds=3), clock=clock)
    with pytest.raises(LimitError):
        b.reserve_batch([{}] * 9, [], False)
    b.reserve_batch([{}] * 8, ["same"] * 20, False)
    assert b.checkpoint()["planning_rounds_used"] == 0
    with pytest.raises(LimitError):
        b.reserve_batch([{}], [str(i) for i in range(13)], True)
    assert b.checkpoint()["planning_rounds_used"] == 1
    for _ in range(2):
        b.reserve_batch([{}], [], True)
    with pytest.raises(LimitError):
        b.reserve_batch([{}], [], False)
    clock.advance(400)
    restored = TurnBudget(
        Settings(_env_file=None, planning_rounds=3), snapshot=b.checkpoint(), clock=FakeClock(99999)
    )
    assert restored.remaining_seconds() == 200
    assert restored.checkpoint()["planning_rounds_used"] == 3
