"""Versioned travel state and decisions. Only the harness changes status or IDs."""

import re
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from trip_assistant.trips.contracts import validate_constraints

DOMAINS = {"train", "hotel"}
SOURCES = {"user", "context", "preference", "derived", "default", "proposal"}
CONDITION_KEYS = {
    "start_date",
    "end_date",
    "departure_date",
    "check_in",
    "check_out",
    "arrival_date",
    "arrival_before",
    "hotel_keywords",
    "passengers",
    "guests",
    "constraints",
    "nights",
    "flexible_dates",
    "hotel_quote_required",
}
PURPOSES = {"visit", "business", "return", "transit", "unspecified"}


def identifier(prefix):
    return f"{prefix}_{uuid4().hex}"


def validate_conditions(value):
    if not isinstance(value, dict) or set(value) - CONDITION_KEYS:
        raise ValueError("unsupported workflow conditions")
    for key, item in value.items():
        if item is None:
            continue
        if key in {"passengers", "guests", "nights"}:
            if type(item) is not int or item < 1:
                raise ValueError("invalid person count or nights")
        elif key.endswith("_date") or key in {"check_in", "check_out"}:
            if not isinstance(item, str):
                raise ValueError("invalid date")
            date.fromisoformat(item)
        elif key in {"flexible_dates", "hotel_quote_required"}:
            if type(item) is not bool:
                raise ValueError("invalid boolean condition")
        elif key == "arrival_before":
            stamp = datetime.fromisoformat(item)
            if stamp.tzinfo is None:
                raise ValueError("arrival deadline needs timezone")
        elif key == "hotel_keywords":
            if not isinstance(item, str) or not item.strip():
                raise ValueError("invalid hotel keywords")
        elif key == "constraints":
            if not isinstance(item, dict):
                raise ValueError("invalid constraints")
            ordinary = {k: v for k, v in item.items() if k != "total_budget_cny"}
            validate_constraints(ordinary)
            if "total_budget_cny" in item:
                amount = Decimal(str(item["total_budget_cny"]))
                if not amount.is_finite() or amount < 0:
                    raise ValueError("invalid total budget")
    return deepcopy(value)


def effective_conditions(confirmed, task_conditions, preferences):
    validate_conditions(confirmed)
    validate_conditions(task_conditions)
    effective = {k: deepcopy(v) for k, v in confirmed.items() if v is not None}
    sources = {k: "context" for k in effective}
    for key, val in task_conditions.items():
        if val is not None:
            if key == "constraints":
                effective[key] = {**effective.get(key, {}), **val}
            else:
                effective[key] = deepcopy(val)
            sources[key] = "user"
    effective.setdefault("constraints", {})
    if "passengers" not in effective:
        effective["passengers"] = 1
        sources["passengers"] = "default"
    if "guests" not in effective:
        effective["guests"] = effective["passengers"]
        sources["guests"] = "derived"
    return effective, sources


def validate_quote_requirement(conditions, context):
    if not conditions.get("hotel_quote_required") or "original_query" not in context:
        return
    if context.get("effective_preferences", {}).get("hotel_quote_required") is True:
        return
    # A model-authored source label is not evidence of a user requirement.
    clauses = re.split(r"[，。；！\n,;.!]", context["original_query"])
    for clause in clauses:
        if re.search(
            r"房价|(?:酒店|住宿|hotel)(?:(?!火车|车票|航班).){0,12}(?:报价|价格|价钱|费用|price|quote|rate)",
            clause,
            re.I,
        ):
            if not re.search(
                r"不用|不要|无需|不需要|不关心|不重要|不核实|not required|no need", clause, re.I
            ):
                return
    raise ValueError("hotel quote requirement lacks explicit user or trusted preference evidence")


def create_workflow(proposal, context, *, workflow_id=None, require_stay_proposals=False):
    if (
        not isinstance(proposal, dict)
        or not isinstance(proposal.get("tasks"), list)
        or not proposal["tasks"]
    ):
        raise ValueError("workflow needs ordered tasks")
    from trip_assistant.providers.rail_preferences import validate_rail_preferences

    rail_preferences = validate_rail_preferences(proposal.get("rail_preferences", {}))
    confirmed = deepcopy(proposal.get("confirmed_conditions", {}))
    if not isinstance(confirmed, dict):
        raise ValueError("invalid confirmed conditions")
    rows = proposal["tasks"]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("task proposal must be an object")
    # These model metadata fields duplicate the route/scope, not query constraints.
    # Check consistency before projecting onto the canonical condition contract.
    route = [r.get("destination") for r in rows if isinstance(r, dict)]
    if "origin" in confirmed and confirmed.pop("origin") != rows[0].get("origin"):
        raise ValueError("route metadata origin mismatch")
    if "destination" in confirmed:
        destination = confirmed.pop("destination")
        if len(rows) != 1 or destination != rows[0]["destination"]:
            raise ValueError("route metadata destination mismatch")
    if "requires_hotel" in confirmed:
        hotel = confirmed.pop("requires_hotel")
        if type(hotel) is not bool or hotel != any(r.get("requires_hotel") for r in rows):
            raise ValueError("hotel scope metadata mismatch")
    if "scope" in confirmed and confirmed.pop("scope") not in {
        "交通与住宿",
        "火车和酒店",
        "train_hotel",
    }:
        raise ValueError("unsupported scope metadata")
    if "return_to" in confirmed and confirmed.pop("return_to") != rows[-1].get("destination"):
        raise ValueError("route metadata return mismatch")
    if "destinations" in confirmed:
        destinations = confirmed.pop("destinations")
        if destinations not in (
            route,
            route[:-1] if route and route[-1] == rows[0].get("origin") else route,
        ):
            raise ValueError("route metadata destinations mismatch")
    constraints = confirmed.get("constraints", {})
    if isinstance(constraints, dict):
        for key in ("only_train_hotel", "no_weather", "no_guide"):
            if key in constraints and constraints.pop(key) is not True:
                raise ValueError("workflow scope metadata mismatch")
    confirmed = validate_conditions(confirmed)
    from trip_assistant.trips.strengths import (
        validate_condition_strengths,
        validate_inherited_strengths,
    )

    strengths = validate_condition_strengths(proposal.get("condition_strengths", {}), confirmed)
    validate_quote_requirement(confirmed, context)
    effective, sources = effective_conditions(
        confirmed, {}, context.get("effective_preferences", {})
    )
    sources.update({k: "user" for k, v in confirmed.items() if v is not None})
    tasks = []
    for index, row in enumerate(proposal["tasks"]):
        if not isinstance(row, dict) or set(row) - {
            "origin",
            "destination",
            "requires_hotel",
            "purpose",
            "purpose_source",
            "conditions",
            "field_sources",
            "condition_strengths",
            "depends_on",
        }:
            raise ValueError("invalid task proposal")
        if (
            not isinstance(row.get("destination"), str)
            or not row["destination"].strip()
            or (
                row.get("origin") is not None
                and (not isinstance(row["origin"], str) or not row["origin"].strip())
            )
        ):
            raise ValueError("task route needs cities")
        default_origin = None
        if index == 0 and row.get("origin") is None:
            home = context.get("effective_preferences", {}).get("home_location")
            if isinstance(home, str) and home.strip() and home.strip() != row["destination"]:
                default_origin = home.strip()
                row = {**row, "origin": default_origin}
        if row.get("origin") == row["destination"]:
            target = (
                f"tasks[{index - 1}]"
                if tasks and tasks[-1]["destination"] == row["destination"]
                else "抵达该城市的task"
            )
            raise ValueError(
                f"route is not continuous: tasks[{index}]为"
                f"{row['origin']}→{row['destination']}同城段；"
                f"将其住宿conditions与hotel item合并到{target}，设置requires_hotel=true，"
                "保留该抵达段的train item并删除独立住宿task；不修改已有硬条件"
            )
        if tasks and row.get("origin") is not None and row["origin"] != tasks[-1]["destination"]:
            raise ValueError(
                f"route is not continuous: tasks[{index}].origin={row['origin']}与"
                f"tasks[{index - 1}].destination={tasks[-1]['destination']}不一致；"
                "检查旅程顺序与城市衔接，不能擅自修改用户硬条件"
            )
        if tasks and row.get("origin") is None:
            row = {**row, "origin": tasks[-1]["destination"]}
        purpose = row.get("purpose", "unspecified")
        if purpose == "unspecified" and row.get("purpose_source") == "unspecified":
            row = {**row, "purpose_source": "proposal"}
        if purpose not in PURPOSES or row.get("purpose_source", "proposal") not in SOURCES:
            raise ValueError("invalid task purpose")
        # Optional proposal dependencies refer to prior task indices; IDs are runtime-owned.
        if row.get("depends_on", []) not in ([], [index - 1] if index else []):
            raise ValueError("invalid dependency")
        if type(row.get("requires_hotel")) is not bool:
            raise ValueError("task hotel scope is required")
        conditions = validate_conditions(row.get("conditions", {}))
        task_strengths = validate_condition_strengths(
            row.get("condition_strengths", {}), {**confirmed, **conditions}
        )
        if require_stay_proposals and row["requires_hotel"]:
            stay = {**confirmed, **conditions}
            dated = (
                stay.get("check_in")
                and stay.get("check_out")
                and date.fromisoformat(stay["check_out"]) > date.fromisoformat(stay["check_in"])
            )
            if not stay.get("nights") and not dated:
                raise ValueError(
                    f"tasks[{index}].conditions needs a model stay proposal: "
                    "requires_hotel=true时需nights或check_in/check_out；"
                    "依据用户要求或由模型提出软安排，程序不默认晚数"
                )
        validate_quote_requirement(conditions, context)
        field_sources = row.get("field_sources", {})
        if not isinstance(field_sources, dict):
            raise ValueError("invalid field source")
        field_sources = deepcopy(field_sources)
        for key, source in list(field_sources.items()):
            if key.startswith("conditions."):
                ordinary = key.removeprefix("conditions.")
                if ordinary in field_sources and field_sources[ordinary] != source:
                    raise ValueError("conflicting field sources")
                field_sources[ordinary] = field_sources.pop(key)
        for key, source in list(field_sources.items()):
            if source == "missing" and row.get(key, conditions.get(key)) is None:
                field_sources.pop(key)
        if any(v not in SOURCES for v in field_sources.values()):
            raise ValueError("invalid field source")
        if (
            require_stay_proposals
            and row["requires_hotel"]
            and any(
                conditions.get(key) is not None and field_sources.get(key) == "default"
                for key in ("nights", "check_in", "check_out")
            )
        ):
            raise ValueError(
                "stay proposal must come from model reasoning or known conditions"
                ", not default values"
            )
        if conditions.get("hotel_quote_required") is True and field_sources.get(
            "hotel_quote_required"
        ) in {"derived", "default", "proposal"}:
            raise ValueError(
                "hotel quote requirement must come from explicit user, context or"
                " preference; needing a hotel does not require a quote"
            )
        if index == 0 and not conditions.get("departure_date") and confirmed.get("start_date"):
            conditions["departure_date"] = confirmed["start_date"]
            field_sources["departure_date"] = "derived"
        task_sources = {k: field_sources.get(k, "proposal") for k in conditions}
        if default_origin is not None:
            task_sources["origin"] = "preference"
        tasks.append(
            dict(
                id=identifier("task"),
                revision=1,
                status="pending",
                origin=row.get("origin"),
                destination=row["destination"],
                purpose=purpose,
                purpose_source=row.get("purpose_source", "proposal"),
                requires_hotel=row["requires_hotel"],
                depends_on=[tasks[-1]["id"]] if tasks else [],
                conditions=conditions,
                field_sources=task_sources,
                condition_strengths=task_strengths,
                draft_plan=None,
                summary="",
                query_ids=[],
                issues=[],
            )
        )
    query = context.get("original_query", "")
    one_way = re.search(
        r"单程|(?:不用|不要|不需要|不安排)(?:安排)?(?:回程|返程)|(?:暂时|先)?不回|不返|one[ -]way",
        query,
        re.I,
    )
    home = context.get("effective_preferences", {}).get("home_location")
    return_to = home.strip() if isinstance(home, str) and home.strip() else tasks[0]["origin"]
    if (
        context.get("default_round_trip")
        and not one_way
        and return_to
        and tasks[-1]["destination"] != return_to
        and tasks[-1]["purpose"] != "return"
    ):
        tasks.append(
            dict(
                id=identifier("task"),
                revision=1,
                status="pending",
                origin=tasks[-1]["destination"],
                destination=return_to,
                purpose="return",
                purpose_source="default",
                requires_hotel=False,
                depends_on=[tasks[-1]["id"]],
                conditions={"flexible_dates": True},
                field_sources={"flexible_dates": "default"},
                draft_plan=None,
                summary="",
                query_ids=[],
                issues=[],
            )
        )
    result = dict(
        id=workflow_id or identifier("workflow"),
        revision=1,
        status="running",
        original_query=context.get("original_query", ""),
        confirmed_conditions=confirmed,
        effective_conditions=effective,
        field_sources=sources,
        condition_strengths=strengths,
        current_task_id=tasks[0]["id"],
        tasks=tasks,
        results_by_query={},
        candidate_cache={},
        checkpoint=None,
        stop_reason=None,
        validation=None,
        audit={"model_calls": 0, "tool_calls": 0, "info_executions": 0},
    )
    validate_inherited_strengths(result)
    result["rail_preferences"] = rail_preferences
    result["effective_preferences"] = {
        **deepcopy(context.get("effective_preferences", {})),
        "rail_preferences": rail_preferences,
    }
    return result


def task_by_id(workflow, task_id):
    task = next((t for t in workflow["tasks"] if t["id"] == task_id), None)
    if task is None:
        raise ValueError("unknown task")
    return task
