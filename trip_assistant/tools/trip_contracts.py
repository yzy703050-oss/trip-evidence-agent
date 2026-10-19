"""Operation-specific native contracts, sharing the canonical field definitions."""

from copy import deepcopy
from typing import Annotated, Literal

from pydantic import Field, StrictInt, create_model

from trip_assistant.trips.contracts import Input, TaskInput, TripPlanInput
from trip_assistant.trips.parameter_schema import CONDITIONS


def subset(name, source, fields, **overrides):
    definitions = {
        key: (source.model_fields[key].annotation, deepcopy(source.model_fields[key]))
        for key in fields.split()
    }
    return create_model(name, __base__=Input, **{**definitions, **overrides})


CreateTask = subset(
    "CreateTask",
    TaskInput,
    "origin destination purpose requires_hotel conditions field_sources "
    "condition_strengths items content",
)
SelectTask = subset(
    "SelectTask", TaskInput, "expected_revision selection_refs components", task_id=(str, ...)
)
AmendTask = TaskInput
TARGET = {"trip_plan_id": (str, ...), "expected_revision": (StrictInt, ...)}
CreatePlan = subset(
    "CreatePlan",
    TripPlanInput,
    "confirmed_conditions field_sources condition_strengths default_round_trip reason allow_repair",
    operation=(Literal["propose"], "propose"),
    tasks=(list[CreateTask], Field(min_length=1)),
)
SelectPlan = subset(
    "SelectPlan",
    TripPlanInput,
    "trip_plan_id expected_revision allow_repair reason",
    operation=(Literal["select"], "select"),
    tasks=(list[SelectTask], Field(default_factory=list)),
    **TARGET,
)
AmendPlan = subset(
    "AmendPlan",
    TripPlanInput,
    "trip_plan_id expected_revision confirmed_conditions field_sources condition_strengths "
    "route_tasks repair_issue_codes allow_repair reason",
    operation=(Literal["amend"], "amend"),
    tasks=(list[AmendTask], Field(default_factory=list)),
    **TARGET,
)
ControlPlan = subset(
    "ControlPlan",
    TripPlanInput,
    "trip_plan_id expected_revision reason",
    operation=(Literal["confirm", "pause", "cancel"], ...),
    **TARGET,
)
NativePlan = Annotated[
    CreatePlan | SelectPlan | AmendPlan | ControlPlan, Field(discriminator="operation")
]


class SharedPlanConditions(Input):
    """Only genuinely shared fields; departure dates belong to individual tasks."""

    end_date: str | None = None
    passengers: int | None = Field(default=None, ge=1)
    guests: int | None = Field(default=None, ge=1)
    hotel_quote_required: bool | None = None
    constraints: dict = Field(
        default_factory=dict, json_schema_extra=CONDITIONS["properties"]["constraints"]
    )


class SinglePassCreatePlan(CreatePlan):
    confirmed_conditions: SharedPlanConditions = Field(default_factory=SharedPlanConditions)


SinglePassPlan = Annotated[
    SinglePassCreatePlan | SelectPlan | AmendPlan | ControlPlan, Field(discriminator="operation")
]
ReviewPlan = Annotated[SelectPlan | AmendPlan, Field(discriminator="operation")]

PrePolicyCreateTask = create_model(
    "PrePolicyCreateTask",
    __base__=CreateTask,
    purpose=(Literal["visit", "return", "transit", "unspecified"], "unspecified"),
)
PrePolicyCreatePlan = create_model(
    "PrePolicyCreatePlan",
    __base__=CreatePlan,
    tasks=(list[PrePolicyCreateTask], Field(min_length=1)),
)
PrePolicyPlan = Annotated[
    PrePolicyCreatePlan | SelectPlan | AmendPlan | ControlPlan, Field(discriminator="operation")
]
