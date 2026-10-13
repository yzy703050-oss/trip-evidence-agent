"""Read persisted facts without running providers or advancing workflow state."""

from copy import deepcopy
from agents.workflow_contracts import task_by_id
from agents.workflow_queries import as_result, eligible_result
from travel_data.candidates import candidate_view
from agents.workflow_guard import check_task


def read_saved_trip(workflow, request):
    revision = request.get("expected_workflow_revision", workflow["revision"])
    if revision != workflow["revision"]:
        raise ValueError("stale read revision")
    limit, offset = request.get("limit", 5), request.get("offset", 0)
    if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or offset < 0:
        raise ValueError("invalid read page")
    if request.get("domain") not in (None, "train", "hotel"):
        raise ValueError("invalid read domain")
    tasks = (
        [task_by_id(workflow, request["task_id"])] if request.get("task_id") else workflow["tasks"]
    )
    pages = []
    for task in tasks:
        for row in workflow["results_by_query"].values():
            if row["task_id"] != task["id"] or (
                request.get("domain") and row["domain"] != request["domain"]
            ):
                continue
            records = [row]
            if request.get("include_history"):
                records += row.get("history", [])
            for record in records:
                valid = (
                    record is row
                    and row["task_revision"] == task["revision"]
                    and not row.get("needs_revalidation")
                    and row["status"] in {"ok", "partial"}
                )
                if not valid and not request.get("include_history"):
                    continue
                result = eligible_result(record, task) if valid else as_result(record)
                full = candidate_view(
                    result,
                    limit=max(1, len(record["items"])),
                    offset=0,
                    constraints=record.get("constraints", {}) if valid else {},
                    preferences={},
                )
                total = full.get("candidate_total", len(full["items"]))
                offsets = range(offset, total, limit) if request.get("all") else [offset]
                for start in offsets:
                    pages.append(
                        {
                            "task_id": task["id"],
                            "query_id": record["id"],
                            "domain": record["domain"],
                            "result_revision": record["result_revision"],
                            "valid": valid,
                            "fetched_at": record.get("fetched_at"),
                            "source": deepcopy(record.get("source")),
                            "candidate_total": total,
                            "view_offset": start,
                            "has_more": start + limit < total,
                            "items": deepcopy(full["items"][start : start + limit]),
                        }
                    )
    return {
        "workflow_id": workflow["id"],
        "workflow_revision": workflow["revision"],
        "status": workflow["status"],
        "tasks": [
            {
                k: deepcopy(t.get(k))
                for k in (
                    "id",
                    "origin",
                    "destination",
                    "purpose",
                    "status",
                    "revision",
                    "draft_plan",
                    "validated_plan",
                )
            }
            for t in tasks
        ],
        "saved_facts": [check_task(workflow, t["id"], t.get("draft_plan")) for t in tasks],
        "pages": pages,
        "missing_fields": [] if pages or not request.get("domain") else ["saved_candidates"],
        "inventory_scope": "saved_candidates",
    }


def render_saved_trip(result):
    lines = ["已保存的行程资料（报价与库存以获取时为准）："]
    for task, checked in zip(result["tasks"], result["saved_facts"]):
        plan = checked["reconstructed_plan"]
        lines.append(f"{task['origin'] or '起点待补'} → {task['destination']}：{task['status']}")
        if plan.get("train"):
            lines.append(
                f"火车 {plan['train']['train_number']}，{plan['schedule'].get('departure_at') or '出发时间未知'} → {plan['schedule'].get('arrival_at') or '到达时间未知'}"
            )
        if plan.get("hotel"):
            lines.append(
                f"酒店 {plan['hotel']['hotel_name']}，入住 {plan['schedule'].get('check_in')}，离店 {plan['schedule'].get('check_out')}"
            )
    for page in result["pages"]:
        lines.append(
            f"{page['domain']} 候选：共保存 {page['candidate_total']} 条，起始位置 {page['view_offset']}，{'当前有效结果' if page['valid'] else '历史或失效结果'}；获取时间 {page['fetched_at'] or '未知'}"
        )
        for item in page["items"]:
            name = item.get("train_number") or item.get("hotel_name") or item.get("id")
            amount = item.get("price_cny") or item.get("stay_total_cny")
            lines.append(
                f"- {name}"
                + (f"，{amount} 元" if amount is not None else "，价格未核实")
                + (f"，{item['address']}" if item.get("address") else "")
            )
        if page["has_more"]:
            lines.append(f"下一页 offset={page['view_offset'] + len(page['items'])}")
    return "\n".join(lines)
