"""Native TripPlan business mutations: preview, selection, validation and save."""

from copy import deepcopy
from datetime import datetime
from decimal import Decimal

from trip_assistant.runtime.errors import ToolFailure
from trip_assistant.storage.trips import WorkflowConflictError
from trip_assistant.trips.activities import apply_content, bind_user_times
from trip_assistant.trips.conditions import (
    create_workflow,
    effective_conditions,
    task_by_id,
    validate_conditions,
)
from trip_assistant.trips.conflicts import find_hard_date_conflict
from trip_assistant.trips.constraints import bind_constraints
from trip_assistant.trips.contracts import TripPlanInput
from trip_assistant.trips.dependencies import affected_components, invalidate_components
from trip_assistant.trips.evidence import evidence_is_fresh
from trip_assistant.trips.state import planning_stage
from trip_assistant.trips.strengths import (
    condition_is_hard,
    condition_value,
    equivalent,
    global_is_hard,
)
from trip_assistant.trips.validation import check_task, check_workflow, finalize_workflow


class TripService:
    def normalize_input(self, tripplan):
        from trip_assistant.trips.patches import normalize_trip_patch

        if tripplan.get("operation") != "amend":
            return tripplan
        saved = self.store.load(tripplan["trip_plan_id"])
        if not saved:
            raise ValueError("trip not found")
        return normalize_trip_patch(saved, tripplan)

    def __init__(self, store, queries, scheduler):
        self.store = store
        self.queries = queries
        self.scheduler = scheduler

    def preview(self, tripplan, turn):
        args = TripPlanInput.model_validate(tripplan).model_dump(exclude_unset=True)
        operation = args.get("operation", "propose")
        previous = None
        if operation == "propose":
            policy = turn.get("policy_context")
            rows = [
                {
                    k: deepcopy(v)
                    for k, v in row.items()
                    if k
                    in {
                        "origin",
                        "destination",
                        "purpose",
                        "requires_hotel",
                        "conditions",
                        "field_sources",
                    }
                }
                for row in args["tasks"]
            ]
            for row in rows:
                row.setdefault("requires_hotel", False)
            # General strengths are stored independently; legacy constructor validates dates only.
            work = create_workflow(
                {"confirmed_conditions": args.get("confirmed_conditions", {}), "tasks": rows},
                {**turn, "default_round_trip": args.get("default_round_trip", True)},
                require_stay_proposals=True,
            )
            work.update(
                user_id=self.store.user_id,
                session_id=turn.get("session_id"),
                condition_strengths=deepcopy(args.get("condition_strengths", {})),
            )
            work["policy_context"] = deepcopy(policy) if policy else None
            for task, row in zip(work["tasks"], args["tasks"], strict=False):
                task["content"] = apply_content([], row.get("content", []))
                bind_user_times(
                    task["content"],
                    query=turn.get("original_query", ""),
                    departure_date=task["conditions"].get("departure_date"),
                    current_time=turn.get("current_time"),
                    enabled=not (
                        turn.get("single_pass_planning")
                        or turn.get("model_managed_activity_times")
                    ),
                )
                task["condition_strengths"] = deepcopy(row.get("condition_strengths", {}))
                task["field_sources"].update(row.get("field_sources", {}))
            work["field_sources"].update(args.get("field_sources", {}))
            self._check_inheritance(work)
            if len(work["tasks"]) != len(rows):
                raise ValueError(
                    "return task needs model departure_date and train item in tasks; "
                    "默认往返必须显式提出返程段，日期和车次查询由模型安排；单程用default_round_trip=false"
                )
        else:
            previous = self.store.load(args["trip_plan_id"])
            if not previous:
                raise ValueError("trip not found")
            if previous["revision"] != args["expected_revision"]:
                raise WorkflowConflictError("trip revision changed")
            codes = args.get("repair_issue_codes", [])
            if codes:
                issues = (previous.get("validation") or {}).get("issues", [])
                selected = [i for i in issues if i["code"] in codes]
                related = {identity for i in selected for identity in i.get("task_ids", [])}
                targets = {r.get("task_id") for r in args.get("tasks", [])}
                if set(codes) - {i["code"] for i in selected} or not targets or targets - related:
                    raise ValueError("unrelated repair target or stale conflict")
            work = deepcopy(previous)
            work.pop("stop_reason", None)
            work.pop("stop_explanation", None)
            work.pop("committed", None)
            work.pop("working", None)
            query = turn.get("original_query", "")
            for key, level in args.get("condition_strengths", {}).items():
                if level == "soft" and global_is_hard(work, key):
                    raise ValueError("cannot soften hard global condition")
            global_changes = {
                k: v
                for k, v in args.get("confirmed_conditions", {}).items()
                if not equivalent(work["confirmed_conditions"].get(k), v, k)
            }
            work["confirmed_conditions"].update(global_changes)
            work.setdefault("condition_strengths", {}).update(args.get("condition_strengths", {}))
            if args.get("route_tasks"):
                args["tasks"] = self._replace_route(work, args["route_tasks"], turn)
            for row in args.get("tasks", []):
                task = task_by_id(work, row.get("task_id"))
                if row.get("expected_revision", task["revision"]) != task["revision"]:
                    raise WorkflowConflictError("task revision changed")
                for key, level in row.get("condition_strengths", {}).items():
                    if level == "soft" and condition_is_hard(work, task, key):
                        raise ValueError("cannot soften hard task condition")
                affected = affected_components(task, row)
                scope = set(row.get("components") or affected or row.get("selection_refs", {}))
                if "content" in row:
                    updated_content = apply_content(task.get("content", []), row["content"])
                    if (
                        updated_content != task.get("content", [])
                        and row.get("components")
                        and "content" not in scope
                    ):
                        raise ValueError("activity change exceeds component scope")
                    task["content"] = updated_content
                    bind_user_times(
                        task["content"],
                        query=query,
                        departure_date=task["conditions"].get("departure_date"),
                        current_time=turn.get("current_time"),
                        enabled=not (
                            turn.get("single_pass_planning")
                            or turn.get("model_managed_activity_times")
                        ),
                    )
                if "schedule" in scope:
                    scope.update({"train", "hotel"})
                if affected - scope:
                    raise ValueError("change exceeds component scope")
                for domain in row.get("selection_refs", {}):
                    if scope and domain not in scope:
                        raise ValueError("selection exceeds component scope")
                task["conditions"].update(row.get("conditions", {}))
                validate_conditions(task["conditions"])
                task.setdefault("condition_strengths", {}).update(
                    row.get("condition_strengths", {})
                )
                task.setdefault("field_sources", {}).update(row.get("field_sources", {}))
                for key in ("origin", "destination", "requires_hotel", "purpose"):
                    if key in row:
                        task[key] = row[key]
                invalidate_components(work, task, affected)
            if global_changes:
                for task in work["tasks"]:
                    invalidate_components(
                        work,
                        task,
                        affected_components({"conditions": {}}, {"conditions": global_changes}),
                    )
            work["validation"] = None
            work["status"] = "running"
            self._check_inheritance(work)
        if operation not in {"pause", "cancel"} and any(
            t.get("purpose") == "business" for t in work["tasks"]
        ):
            from trip_assistant.agents.policy import resolve_policy_for_workflow

            policy = turn.get("policy_context") or work.get("policy_context")
            if policy:
                policy = resolve_policy_for_workflow(policy, work, turn)
            work["policy_context"] = policy
        bind_constraints(work, previous, verifier=turn.get("decision_role") == "verifier")
        rows = args.get("tasks", [])
        if turn.get("current_time") and operation in {"propose", "amend"}:
            now = datetime.fromisoformat(turn["current_time"])
            for index, row in enumerate(rows):
                task = (
                    work["tasks"][index]
                    if operation == "propose"
                    else task_by_id(work, row.get("task_id"))
                )
                if operation == "amend" and "departure_date" not in row.get("conditions", {}):
                    continue
                departure = task["conditions"].get("departure_date")
                if departure and departure < now.date().isoformat():
                    raise ToolFailure(
                        "DEPARTURE_IN_PAST",
                        "departure_date is earlier than current date",
                        {
                            "task_id": task["id"],
                            "departure_date": departure,
                            "current_time": turn["current_time"],
                        },
                    )
        requests = []
        keys = []
        work.setdefault("pending_queries", [])
        hard_conflict = find_hard_date_conflict(work)
        for row, task in (
            (
                row,
                work["tasks"][i]
                if operation == "propose"
                else task_by_id(work, row.get("task_id")),
            )
            for i, row in enumerate(rows)
        ):
            if row.get("items"):
                domains = {item["domain"] for item in row["items"]}
                task["items"] = [i for i in task.get("items", []) if i["domain"] not in domains]
            for raw_item in row.get("items", []):
                item = self._effective_item(work, task, raw_item)
                task["items"].append(deepcopy(item))
                if item["domain"] == "hotel" and not task["requires_hotel"]:
                    raise ValueError("hotel outside task scope")
                dependencies = []
                for dep in item.get("depends_on", []):
                    if dep == "previous":
                        index = work["tasks"].index(task)
                        if not index:
                            raise ValueError("first task has no previous dependency")
                        dep = work["tasks"][index - 1]["id"]
                    dependencies.append(task_by_id(work, dep))
                waiting = any(
                    not t.get("draft_plan")
                    or not check_task(work, t["id"], t["draft_plan"])["valid"]
                    for t in dependencies
                )
                parameters = item["parameters"]
                missing = item["domain"] == "train" and (
                    not parameters.get("origin")
                    or not parameters.get("destination")
                    or not any(
                        parameters.get(k)
                        for k in ("departure_date", "arrival_date", "arrival_before")
                    )
                )
                work["pending_queries"] = [
                    p
                    for p in work["pending_queries"]
                    if not (p["task_id"] == task["id"] and p["item"]["domain"] == item["domain"])
                ]
                task.setdefault("query_containers", {}).setdefault(
                    item["domain"],
                    {"status": "empty", "current_query_ref": None, "history_refs": []},
                )
                if hard_conflict or waiting or missing:
                    work["pending_queries"].append(
                        {
                            "task_id": task["id"],
                            "item": item,
                            "reason": "hard_date_conflict"
                            if hard_conflict
                            else "waiting_dependency"
                            if waiting
                            else "missing_query_parameters",
                        }
                    )
                    continue
                try:
                    expanded = self.queries.expand_queries({"query_plan": [item]}, turn)
                except ToolFailure as error:
                    if error.code != "MISSING_QUERY_PARAMETERS":
                        raise
                    work["pending_queries"].append(
                        {
                            "task_id": task["id"],
                            "item": item,
                            "reason": str(error),
                            "error": {"code": error.code, "details": error.details},
                        }
                    )
                    continue
                keys.extend(expanded)
                requests.append((task["id"], item))
            for domain, ref in row.get("selection_refs", {}).items():
                self._select(work, task, domain, ref, turn)
        return {
            "arguments": args,
            "previous": previous,
            "working": work,
            "requests": requests,
            "query_keys": keys,
        }

    def _replace_route(self, work, rows, turn):
        used = set()
        proposals, matches = [], []
        for row in rows:
            matched = task_by_id(work, row["task_id"]) if row.get("task_id") else None
            if matched is None:
                options = [
                    t
                    for t in work["tasks"]
                    if t["id"] not in used
                    and (t["origin"], t["destination"])
                    == (row.get("origin"), row.get("destination"))
                ]
                if len(options) == 1:
                    matched = options[0]
            if matched:
                if (
                    matched["id"] in used
                    or row.get("expected_revision", matched["revision"]) != matched["revision"]
                ):
                    raise WorkflowConflictError("route task revision or identity changed")
                used.add(matched["id"])
            base = {
                k: deepcopy(v)
                for k, v in (matched or {}).items()
                if k
                in {
                    "origin",
                    "destination",
                    "purpose",
                    "requires_hotel",
                    "conditions",
                    "field_sources",
                    "condition_strengths",
                }
            }
            base.update(
                {
                    k: deepcopy(v)
                    for k, v in row.items()
                    if k
                    in {
                        "origin",
                        "destination",
                        "purpose",
                        "requires_hotel",
                        "conditions",
                        "field_sources",
                        "condition_strengths",
                    }
                }
            )
            base.setdefault("requires_hotel", False)
            proposals.append(base)
            matches.append(matched)
        replacement = create_workflow(
            {
                "confirmed_conditions": work["confirmed_conditions"],
                "condition_strengths": work.get("condition_strengths", {}),
                "tasks": proposals,
            },
            {**turn, "default_round_trip": False},
            workflow_id=work["id"],
            require_stay_proposals=True,
        )
        tasks, updates = [], []
        for row, candidate, matched in zip(rows, replacement["tasks"], matches, strict=True):
            task = matched if matched else candidate
            dependency = [tasks[-1]["id"]] if tasks else []
            if matched and task["depends_on"] != dependency:
                task["revision"] += 1
                task["needs_dependency_check"] = True
                for key in task.get("query_ids", []):
                    work["results_by_query"][key]["task_revision"] = task["revision"]
                if task.get("draft_plan"):
                    task["draft_plan"]["task_revision"] = task["revision"]
            task["depends_on"] = dependency
            tasks.append(task)
            updates.append({**row, "task_id": task["id"], "expected_revision": task["revision"]})
        work["tasks"] = tasks
        return updates

    def _check_inheritance(self, work):
        def conflict(task, key, message, inherited_key=None):
            inherited_key = inherited_key or key
            source = "confirmed_conditions." + inherited_key
            inherited_start = inherited_key == "start_date" and key == "departure_date"
            if inherited_start:
                message += (
                    ": task.conditions.departure_date inherits confirmed_conditions.start_date; "
                    "start_date is the whole-trip departure boundary, not an activity date. "
                    "Keep activity dates in content.date and correct misplaced fields without "
                    "relaxing user requirements."
                )
            raise ToolFailure(
                "CONDITION_SCOPE_CONFLICT",
                message,
                {
                    "field": key,
                    "task_id": task["id"],
                    "inherited_from": source,
                    "global_value": condition_value(work["confirmed_conditions"], inherited_key),
                    "task_value": condition_value(task["conditions"], key),
                    "global_strength": "hard" if global_is_hard(work, inherited_key) else "soft",
                    "task_strength": task.get("condition_strengths", {}).get(
                        key,
                        "hard" if task.get("field_sources", {}).get(key) in {"user", "context"}
                        else "soft",
                    ),
                    "global_source": work.get("field_sources", {}).get(inherited_key),
                    "task_source": task.get("field_sources", {}).get(key),
                    "repair_hint": (
                        "先比较global/task的value、source和strength；日期相同也不能将继承hard标为soft。"
                        "用户明确的全局start_date硬边界须保留，首段departure_date继承hard；"
                        "会议日放content.date，不等于出发日。模型自行建议的日期放各task.conditions，"
                        "不要重复生成全局start_date及其元数据。propose尚未保存时可删除误填全局建议，"
                        "但不能删除或软化用户/政策硬要求；已有计划按权限修改或交Main。"
                        if key in {"departure_date", "start_date", "end_date"}
                        else "保留全局硬条件，修正冲突的任务字段或报告真实冲突，"
                        "不放宽用户或政策限制。"
                    ),
                },
            )

        for task in work["tasks"]:
            global_values = work["confirmed_conditions"]
            for key, value in {**global_values, **global_values.get("constraints", {})}.items():
                if key == "constraints" or not global_is_hard(work, key):
                    continue
                local = condition_value(task["conditions"], key)
                if local is None:
                    continue
                if key.endswith("_cny"):
                    incompatible = Decimal(str(local)) > Decimal(str(value))
                else:
                    incompatible = not equivalent(local, value, key)
                if incompatible:
                    conflict(task, key, "hard global condition conflict")
            for key, level in task.get("condition_strengths", {}).items():
                if level not in {"hard", "soft"}:
                    raise ValueError("invalid strength")
                if level == "soft" and (
                    global_is_hard(work, key)
                    or key == "departure_date"
                    and task is work["tasks"][0]
                    and global_is_hard(work, "start_date")
                ):
                    conflict(
                        task,
                        key,
                        "cannot soften inherited hard condition",
                        key if global_is_hard(work, key) else "start_date",
                    )
                if (
                    global_is_hard(work, key)
                    and key in task["conditions"]
                    and task["conditions"][key] != work["confirmed_conditions"][key]
                ):
                    conflict(task, key, "hard global condition conflict")

    def _effective_item(self, work, task, raw):
        item = deepcopy(raw)
        domain = item["domain"]
        parameters = item.setdefault("parameters", {})
        effective, _ = effective_conditions(work["confirmed_conditions"], task["conditions"], {})
        fixed = {
            k: v
            for k, v in effective.items()
            if k != "constraints" and condition_is_hard(work, task, k)
        }
        expected = (
            {
                "origin": task["origin"],
                "destination": task["destination"],
                "passengers": effective["passengers"],
            }
            if domain == "train"
            else {"city": task["destination"], "guests": effective["guests"]}
            if domain == "hotel"
            else {}
        )
        for key, value in expected.items():
            if key in parameters and parameters[key] != value:
                raise ValueError("query conflicts with task route or party")
            parameters[key] = value
        for key in (
            ("departure_date", "arrival_date", "arrival_before")
            if domain == "train"
            else ("check_in", "check_out")
            if domain == "hotel"
            else ()
        ):
            if key in fixed:
                if key in parameters and parameters[key] != fixed[key]:
                    raise ValueError("query conflicts with hard condition")
                parameters[key] = fixed[key]
            elif key not in parameters and key in effective:
                parameters[key] = effective[key]
        constraints = deepcopy(item.get("constraints", {}))
        if (
            domain == "hotel"
            and effective.get("hotel_keywords")
            and condition_is_hard(work, task, "hotel_keywords")
        ):
            constraints["hotel_keywords"] = effective["hotel_keywords"]
        inherited = effective.get("constraints", {})
        for key, value in inherited.items():
            if key == "total_budget_cny":
                continue
            level = task.get("condition_strengths", {}).get(
                key, work.get("condition_strengths", {}).get(key, "hard")
            )
            if level == "hard":
                if item.get("condition_strengths", {}).get(key) == "soft":
                    raise ToolFailure(
                        "QUERY_CONDITION_CONFLICT",
                        "item cannot soften inherited hard constraint: " + key,
                        {
                            "task_id": task["id"], "domain": domain, "field": key,
                            "inherited_value": value, "inherited_strength": level,
                            "item_strength": "soft",
                            "repair_hint": (
                                "该字段在任务/全局未标soft时按hard继承，item不能单独改soft。"
                                "删除item中该字段的重复强度标注以继承hard，或标hard。"
                                "确为模型建议时需在有权限的任务层与item一致标soft，"
                                "不能放宽用户或政策硬条件。"
                            ),
                        },
                    )
                if key in constraints and not equivalent(constraints[key], value, key):
                    raise ValueError("item cannot relax hard constraint")
                constraints[key] = value
        for rule in (work.get("policy_context") or {}).get("constraints", []):
            key = rule.get("field")
            scope = rule.get("applicability", {})
            if scope.get("city") and scope["city"] != task["destination"]:
                continue
            if key and rule.get("hard"):
                current = constraints.get(key)
                constraints[key] = (
                    min(current, rule["value"])
                    if current is not None and key.endswith("_cny")
                    else rule["value"]
                )
                item.setdefault("condition_strengths", {})[key] = "hard"
        # Soft filters express preferences; model ranking, not mandatory exclusion.
        constraints = {
            k: v
            for k, v in constraints.items()
            if item.get("condition_strengths", {}).get(k, "hard") != "soft"
        }
        item["constraints"] = constraints
        return item

    def _select(self, work, task, domain, ref, turn):
        if domain not in {"train", "hotel"}:
            raise ValueError("invalid selected domain")
        if domain == "hotel" and not task["requires_hotel"]:
            raise ValueError("hotel outside task scope")
        row = work["results_by_query"].get(ref.get("query_id"))
        shown = turn.get("shown_candidates", {}).get(ref.get("candidate_id"))
        known = bool(row and ref.get("candidate_id") in row.get("shown_candidate_ids", []))
        if not known and (
            not shown
            or shown.get("query_id") != ref.get("query_id")
            or shown.get("result_revision") != ref.get("result_revision")
        ):
            raise ValueError("candidate must have been shown")
        if row is None:
            raise ValueError(
                "use task-bound query_id from write_trip/read trip; "
                "independent query is not bound to this task"
            )
        if (
            row.get("task_id") == task["id"]
            and row.get("domain") == domain
            and row.get("task_revision") != task["revision"]
        ):
            raise ToolFailure(
                "QUERY_REBIND_REQUIRED",
                "此批未保存。任务条件或约束已变，旧查询绑定失效，不是时间过期。"
                "先amend提交条件及对应领域items，不带该领域selection_refs；"
                "查询完成后按返回的最新任务版本和有效候选选择。",
                {
                    "task_id": task["id"],
                    "domain": domain,
                    "query_id": ref.get("query_id"),
                    "task_revision_after_change": task["revision"],
                    "query_task_revision": row.get("task_revision"),
                },
            )
        if not evidence_is_fresh(row, self.scheduler.settings.evidence_max_age_seconds):
            raise ToolFailure(
                "EVIDENCE_EXPIRED",
                "candidate evidence expired; refresh before selecting",
                {
                    "trip_plan_id": work["id"],
                    "task_id": task["id"],
                    "domain": domain,
                    "query_id": ref.get("query_id"),
                    "result_revision": ref.get("result_revision"),
                },
            )
        from trip_assistant.trips.evidence import resolve_selection

        selected = resolve_selection(work, task["id"], task["revision"], ref)
        if domain == "train" and selected.get("departure_at") and turn.get("current_time"):
            departure = datetime.fromisoformat(selected["departure_at"])
            now = datetime.fromisoformat(turn["current_time"])
            if departure.date() < now.date() or (
                departure.tzinfo is not None and now.tzinfo is not None and departure < now
            ):
                raise ToolFailure(
                    "DEPARTURE_IN_PAST",
                    "selected train departure is earlier than current time",
                    {
                        "task_id": task["id"],
                        "departure_at": selected["departure_at"],
                        "current_time": turn["current_time"],
                    },
                )
        draft = task.setdefault("draft_plan", None) or {
            "task_revision": task["revision"],
            "train_selection": None,
            "hotel_selection": None,
            "schedule": {},
            "unverified_requirements": [],
        }
        draft[domain + "_selection"] = deepcopy(ref)
        draft["task_revision"] = task["revision"]
        draft["schedule"].update(
            {k: v for k, v in task["conditions"].items() if k in {"check_in", "check_out"}}
        )
        task.update(draft_plan=draft, status="draft", draft_stage="selection")

    def expand_queries(self, args, turn):
        self.preview(args["tripplan"], turn)
        return []

    async def apply(self, tripplan, turn):
        operation_id = turn["operation_id"]
        receipt = self.store.lookup_operation(operation_id)
        if receipt:
            return self._result(receipt)
        key = tripplan.get("trip_plan_id") or operation_id
        async with self.scheduler.write_lock(key):
            receipt = self.store.lookup_operation(operation_id)
            if receipt:
                return self._result(receipt)
            prepared = self.preview(tripplan, turn)
            work = prepared["working"]
            args = prepared["arguments"]
            previous = prepared["previous"]
            if (
                args.get("operation") == "amend"
                and previous
                and previous["status"] == "completed"
                and work.get("policy_context") == previous.get("policy_context")
                and not any(
                    args.get(k)
                    for k in (
                        "tasks",
                        "route_tasks",
                        "confirmed_conditions",
                        "field_sources",
                        "condition_strengths",
                        "repair_issue_codes",
                    )
                )
            ):
                receipt = {**previous, "last_operation": "amend"}
                return self._result(self.store.record_unchanged(receipt, operation_id))
            work["last_operation"] = args.get("operation", "propose")
            expected = prepared["previous"]["revision"] if prepared["previous"] else None

            for task_id, item in prepared["requests"]:
                task = task_by_id(work, task_id)
                stale = {
                    ref
                    for ref in task.get("query_ids", [])
                    if work["results_by_query"][ref]["domain"] == item["domain"]
                }
                for ref in stale:
                    work["results_by_query"][ref]["needs_revalidation"] = True
                work["last_candidate_views"] = [
                    view
                    for view in work.get("last_candidate_views", [])
                    if view.get("query_id") not in stale
                ]
                task.update(status="draft", draft_stage="query")
                work["pending_queries"].append(
                    {
                        "task_id": task_id,
                        "item": deepcopy(item),
                        "reason": "awaiting_query",
                    }
                )
            return self.save_result(
                work,
                args,
                expected,
                operation_id,
                goal_assessment=turn.get("goal_assessment"),
                require_goal_assessment=turn.get("require_goal_assessment", False),
            )

    def save_result(
        self,
        work,
        args,
        expected,
        operation_id,
        *,
        goal_assessment=None,
        require_goal_assessment=False,
    ):
        checked = check_workflow(work)
        if work.get("pending_queries"):
            checked["issues"].append(
                {
                    "code": "query_pending",
                    "blocking": True,
                    "message": "查询尚未完成，保留原有效方案与工作进度。",
                }
            )
            checked["valid"] = False
        conflict = find_hard_date_conflict(work)
        if conflict:
            checked["issues"].append(
                {
                    "code": "hard_date_conflict",
                    "task_ids": conflict["affected_task_ids"],
                    "blocking": True,
                    "message": conflict["reason"],
                    "evidence": conflict["evidence_refs"],
                }
            )
            checked["valid"] = False
        impacts = {
            identity
            for i in checked["issues"]
            if i["code"]
            in {
                "departure_before_previous_arrival",
                "departure_before_previous_checkout",
                "route_dependency_mismatch",
            }
            for identity in i.get("task_ids", [])[1:]
        }
        work["dependency_impacts"] = sorted(impacts)
        for task in work["tasks"]:
            if task["id"] in impacts:
                task["needs_dependency_check"] = True
            else:
                task.pop("needs_dependency_check", None)
        operation = args.get("operation", "propose")
        blocking_codes = {i["code"] for i in checked["issues"] if i["blocking"]}
        work["validation"] = checked
        if operation in {"pause", "cancel"}:
            if not args.get("reason"):
                raise ValueError("stop reason is required")
            work.update(
                stop_reason="paused" if operation == "pause" else "cancelled",
                stop_explanation=args["reason"],
                checkpoint={"kind": operation, "reason": args["reason"]},
            )
            saved = self.store.save_working(
                work, expected_revision=expected, operation_id=operation_id
            )
        elif checked["valid"] and operation in {"select", "confirm", "amend"}:
            work = finalize_workflow(work, checked)
            if operation == "confirm":
                work["user_confirmed"] = True
                for task in work["tasks"]:
                    task["user_adopted"] = True
            saved = self.store.commit(work, expected_revision=expected, operation_id=operation_id)
        else:
            blocked = self._hard_conflict(work, checked) and (
                operation not in {"propose", "query"} or conflict is not None
            )
            work.update(
                stop_reason="hard_constraint_conflict" if blocked else "awaiting_model",
                stop_explanation="硬条件无法满足或必要政策资料未核实，已保留可用安排。"
                if blocked
                else "等待主 Agent 选择候选或调整软条件。",
            )
            if conflict:
                work["stop_explanation"] = conflict["reason"]
            elif (
                operation in {"select", "amend", "confirm"}
                and blocking_codes
                and blocking_codes <= {"policy_unverified", "policy_constraint_unverified"}
            ):
                work.update(
                    stop_reason="policy_unverified",
                    stop_explanation="已保留可用安排，企业政策仍有未核实资料，不能确认合规完成。",
                )
            elif self._capability_gap_only(work, checked):
                work.update(
                    stop_reason="provider_capability_missing",
                    stop_explanation="当前酒店地点接口不提供强制预算所需报价，已保留草稿，停止重复查询。",
                )
            elif args.get("allow_repair") is False and operation in {"select", "confirm"}:
                work.update(
                    stop_reason="repair_disabled",
                    stop_explanation="；".join(
                        i["message"] for i in checked["issues"] if i["blocking"]
                    )
                    or "已保留当前可用安排。",
                )
            saved = self.store.save_working(
                work, expected_revision=expected, operation_id=operation_id
            )
        return self._result(saved)

    def _capability_gap_only(self, work, check):
        blocking = [i for i in check["issues"] if i["blocking"]]
        hotel_rules = {
            r["id"]
            for r in (work.get("policy_context") or {}).get("constraints", [])
            if (r.get("field") or "").startswith("hotel_")
        }

        def capability(issue):
            if issue["code"] in {"hotel_price_unknown", "total_budget_unknown"}:
                return True
            return (
                issue["code"] == "policy_constraint_unverified"
                and issue.get("evidence") in hotel_rules
            )

        return (
            bool(blocking)
            and all(capability(i) for i in blocking)
            and any(
                (p.get("hotel") or {}).get("kind") == "hotel_place"
                for p in check["reconstructed_tasks"]
            )
        )

    def _hard_conflict(self, work, check):
        # A bad selected train or unknown policy does not prove hard dates unsatisfiable.
        # Activity validation still blocks commit; the model may repair soft travel dates.
        return any(
            i["blocking"]
            and ("hard" in i["code"] or i["code"] in {"start_date_conflict", "end_date_conflict"})
            for i in check["issues"]
        )

    def _result(self, saved):
        return {
            "status": "completed"
            if saved["status"] == "completed"
            else "needs_input"
            if saved.get("stop_reason") == "needs_input"
            else "partial",
            "operation": saved.get("last_operation"),
            "trip_plan_id": saved["id"],
            "revision": saved["revision"],
            "tasks": [
                {
                    k: deepcopy(t.get(k))
                    for k in (
                        "id",
                        "revision",
                        "status",
                        "origin",
                        "destination",
                        "purpose",
                        "requires_hotel",
                        "conditions",
                        "content",
                        "condition_strengths",
                        "draft_plan",
                    )
                }
                for t in saved["tasks"]
            ],
            "validation": saved.get("validation"),
            "candidates": []
            if saved["status"] == "completed"
            else deepcopy(saved.get("last_candidate_views", [])),
            "committed_revision": (saved.get("committed") or {}).get("revision"),
            "reason": saved.get("stop_explanation"),
            "dependency_impacts": deepcopy(saved.get("dependency_impacts", [])),
            "pending_queries": deepcopy(saved.get("pending_queries", [])),
            "stage": planning_stage(saved),
            "stop_reason": saved.get("stop_reason"),
            "stop_planning": saved.get("stop_reason")
            in {
                "provider_capability_missing",
                "repair_disabled",
                "paused",
                "cancelled",
                "needs_input",
                "hard_constraint_conflict",
                "policy_unverified",
                "goal_not_satisfied",
            },
        }
