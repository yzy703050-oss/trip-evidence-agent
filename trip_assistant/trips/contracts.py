"""Input contracts: models propose business changes, never execution facts."""

import math
import re
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from trip_assistant.trips.parameter_schema import (
    CONDITIONS,
    FILTERS,
    PARAMETERS,
    QUERY_PARAMETERS,
    SELECTIONS,
)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoalAssessment(Input):
    status: Literal["satisfied", "partial", "unmet", "unknown"]
    reason: str = Field(min_length=1, max_length=800)

    @field_validator("reason")
    @classmethod
    def nonempty_reason(cls, value):
        if not value.strip():
            raise ValueError("goal assessment requires a reason grounded in the original query")
        return value.strip()


class Ranking(Input):
    field: Literal[
        "price_cny",
        "stay_total_cny",
        "nightly_cny",
        "departure_at",
        "arrival_at",
        "duration_minutes",
        "train_kind",
        "seat_class",
        "hotel_name",
        "distance_m",
    ]
    direction: Literal["asc", "desc"] = "asc"
    preferred: str | list[str] | None = None


class QueryItem(Input):
    domain: Literal["train", "hotel", "weather", "web"]
    parameters: dict = Field(default_factory=dict, json_schema_extra=QUERY_PARAMETERS)
    constraints: dict = Field(
        default_factory=dict,
        json_schema_extra=FILTERS,
        description="仅填用户或政策明确的硬过滤；软偏好用ranking。未指定车种不要加train_kind。",
    )
    field_sources: dict[str, str] = Field(
        default_factory=dict,
        json_schema_extra={
            "additionalProperties": {
                "type": "string",
                "enum": ["user", "context", "preference", "derived", "default", "proposal"],
            },
            "description": "来源与强度分离；居住地用preference，模型补齐用proposal",
        },
    )
    condition_strengths: dict[str, Literal["hard", "soft"]] = Field(default_factory=dict)
    ranking: list[Ranking] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    refresh: bool = False

    @model_validator(mode="after")
    def valid_query(self):
        allowed = PARAMETERS
        if set(self.parameters) - allowed[self.domain].keys():
            raise ValueError(
                "unsupported query parameters; "
                + self.domain
                + " allows: "
                + ",".join(PARAMETERS[self.domain])
            )
        validate_money(self.constraints)
        validate_constraints(self.constraints)
        return self


CONDITION_KEYS = {
    "origin",
    "destination",
    "start_date",
    "end_date",
    "departure_date",
    "arrival_date",
    "arrival_before",
    "check_in",
    "check_out",
    "nights",
    "passengers",
    "guests",
    "hotel_keywords",
    "constraints",
    "flexible_dates",
    "hotel_quote_required",
}


def validate_money(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_cny") and (
                type(item) not in (int, float) or not math.isfinite(item) or item < 0
            ):
                raise ValueError("money must be a finite nonnegative JSON number")
            validate_money(item)
    elif isinstance(value, list):
        for item in value:
            validate_money(item)
    return value


class ActivityInput(Input):
    id: str | None = None
    date: str | None = None
    time: str | None = Field(default=None, max_length=80)
    purpose: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=1000)
    source: Literal["user", "context", "preference", "derived", "default", "proposal"]
    strength: Literal["hard", "soft"]

    @field_validator("date")
    @classmethod
    def valid_date(cls, value):
        if value is not None:
            date.fromisoformat(value)
        return value


def validate_deadline_source(conditions, sources, strengths):
    if (
        conditions.get("arrival_before")
        and sources.get("arrival_before") in {"derived", "proposal", "default", "preference"}
        and strengths.get("arrival_before", "hard") == "hard"
    ):
        raise ValueError(
            "inferred arrival deadline cannot be hard; preserve the user's coarse activity time "
            "in content and omit an invented arrival_before; never relabel it as user evidence"
        )


class TaskInput(Input):
    task_id: str | None = None
    expected_revision: StrictInt | None = None
    origin: str | None = None
    destination: str | None = None
    purpose: Literal["visit", "business", "return", "transit", "unspecified"] = "unspecified"
    requires_hotel: bool = Field(
        default=False,
        description="是否在目的地住宿；为true时conditions需nights或check_in/check_out。",
    )
    conditions: dict = Field(default_factory=dict, json_schema_extra=CONDITIONS)
    field_sources: dict[str, str] = Field(
        default_factory=dict,
        json_schema_extra={
            "additionalProperties": {
                "type": "string",
                "enum": ["user", "context", "preference", "derived", "default", "proposal"],
            },
            "description": "来源与强度分离；居住地用preference，模型补齐用proposal",
        },
    )
    condition_strengths: dict[str, Literal["hard", "soft"]] = Field(default_factory=dict)
    items: list[QueryItem] = Field(default_factory=list)
    content: list[ActivityInput] = Field(
        default_factory=list,
        description="活动内容；旅游按偏好补proposal/soft，出差只写用户明确活动，空闲留空。"
        "修改活动须保留已有id；省略保留原内容，显式[]清空需保护硬活动。",
    )
    selection_refs: dict = Field(default_factory=dict, json_schema_extra=SELECTIONS)
    components: list[Literal["train", "hotel", "schedule", "content"]] = Field(default_factory=list)

    @field_validator("conditions")
    @classmethod
    def valid_conditions(cls, value):
        if set(value) - CONDITION_KEYS:
            raise ValueError("unsupported task conditions")
        return validate_money(value)

    @model_validator(mode="after")
    def valid_deadline_source(self):
        validate_deadline_source(self.conditions, self.field_sources, self.condition_strengths)
        return self


class TripPlanInput(Input):
    operation: Literal["propose", "select", "amend", "confirm", "pause", "cancel"] = "propose"
    trip_plan_id: str | None = None
    expected_revision: StrictInt | None = None
    confirmed_conditions: dict = Field(default_factory=dict, json_schema_extra=CONDITIONS)
    field_sources: dict[str, str] = Field(
        default_factory=dict,
        json_schema_extra={
            "additionalProperties": {
                "type": "string",
                "enum": ["user", "context", "preference", "derived", "default", "proposal"],
            },
            "description": "来源与强度分离；居住地用preference，模型补齐用proposal",
        },
    )
    condition_strengths: dict[str, Literal["hard", "soft"]] = Field(default_factory=dict)
    tasks: list[TaskInput] = Field(
        default_factory=list,
        description="按顺序列城际段，如重庆→上海含train和hotel、上海→重庆含train；"
        "住宿条件放抵达段conditions，不能独立创建同城住宿task。",
    )
    route_tasks: list[TaskInput] | None = Field(
        default=None, description="完整新路线，仅amend可用，与局部tasks互斥；新增段不填写task_id"
    )
    repair_issue_codes: list[str] = Field(
        default_factory=list, description="自动修复时引用最新校验的冲突代码；目标必须与冲突相关"
    )
    default_round_trip: bool = Field(
        default=True,
        description="默认往返：tasks必须显式包含去程和返程，每段填写departure_date及train item。"
        "只安排单程时false；程序不为遗漏返程自动补日期和选择。",
    )
    allow_repair: bool = True
    reason: str | None = None

    @field_validator("confirmed_conditions")
    @classmethod
    def valid_conditions(cls, value):
        if set(value) - CONDITION_KEYS:
            raise ValueError("unsupported trip conditions")
        return validate_money(value)

    @model_validator(mode="after")
    def valid_target(self):
        validate_deadline_source(
            self.confirmed_conditions, self.field_sources, self.condition_strengths
        )
        if self.repair_issue_codes and (self.operation != "amend" or self.route_tasks is not None):
            raise ValueError("repair requires scoped amend tasks")
        if self.route_tasks is not None and (
            self.operation != "amend" or self.tasks or not self.route_tasks
        ):
            raise ValueError("full route is only valid for amend without partial tasks")
        if self.operation == "propose" and not self.tasks:
            raise ValueError("proposal needs tasks")
        if self.operation != "propose" and (
            not self.trip_plan_id or self.expected_revision is None
        ):
            raise ValueError("existing trip needs identity and expected revision")
        return self


def validate_model_money(value):
    """Only new model outputs require JSON numbers; stored decimal strings remain valid."""

    def check(amount):
        if type(amount) not in {int, float}:
            raise ValueError("model amount must be a finite nonnegative JSON number")
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("model amount must be a finite nonnegative JSON number")

    if isinstance(value, dict):
        constraints = value.get("constraints")
        if isinstance(constraints, dict):
            for key, amount in constraints.items():
                if key.endswith("_cny"):
                    check(amount)
        if value.get("field") in {
            "hotel_max_total_cny",
            "hotel_max_nightly_cny",
            "train_max_total_cny",
            "total_budget_cny",
        }:
            check(value.get("value"))
        for child in value.values():
            validate_model_money(child)
    elif isinstance(value, list):
        for child in value:
            validate_model_money(child)
    return value


FILTER_KEYS = {
    "hotel_max_total_cny",
    "hotel_max_nightly_cny",
    "train_max_total_cny",
    "seat_class",
    "train_kind",
    "departure_time_after",
    "departure_time_before",
    "available_only",
}


def validate_constraints(value):
    if not isinstance(value, dict) or set(value) - FILTER_KEYS:
        raise ValueError("unsupported constraints")
    for key, val in value.items():
        if key.endswith("_cny"):
            amount = Decimal(str(val))
            if not amount.is_finite() or amount < 0:
                raise ValueError("invalid budget")
        elif key == "available_only":
            if type(val) is not bool:
                raise ValueError("invalid inventory constraint")
        elif key == "train_kind":
            if val not in ("high_speed", "emu", "other"):
                raise ValueError("invalid train kind constraint")
        elif not isinstance(val, str) or not val.strip():
            raise ValueError("invalid constraint")
        elif key.startswith("departure_time_") and not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", val
        ):
            raise ValueError("invalid time")
    return value
