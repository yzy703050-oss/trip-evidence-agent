"""Read-only replies bound to the exact TripPlan write receipt."""

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import Field, field_validator

from trip_assistant.runtime.errors import ToolFailure
from trip_assistant.trips.contracts import Input


class TripReply(Input):
    tasks: dict[str, list[Literal["train", "hotel", "content"]]] = Field(default_factory=dict)
    explanation: str = Field(default="", max_length=600, pattern=r"^[^\d]*$")

    @field_validator("tasks")
    @classmethod
    def unique_components(cls, tasks):
        if any(not parts or len(parts) != len(set(parts)) for parts in tasks.values()):
            raise ValueError("每个任务须引用至少一个且不重复的组件")
        return tasks


def validate_trip_reply(reply, plan, saved):
    spec = TripReply.model_validate(reply).model_dump()
    if plan.get("operation") not in {"select", "amend"} or not saved:
        raise ToolFailure("INVALID_REPLY_REFERENCE", "最终reply须用于已有计划的select/amend。")
    if plan.get("route_tasks") and spec["tasks"]:
        raise ToolFailure(
            "INVALID_REPLY_REFERENCE", "路线任务调整请使用reply={}展示保存后的完整计划。"
        )
    known = {t["id"] for t in saved.get("tasks", [])}
    unknown = set(spec["tasks"]) - known
    if unknown:
        raise ToolFailure(
            "INVALID_REPLY_REFERENCE",
            "reply引用未知任务，须使用当前快照的任务ID。",
            {
                "fields": [
                    {"field": "reply.tasks", "message": "未知任务引用", "expected": sorted(known)}
                ]
            },
        )
    return spec


def _stamp(value):
    try:
        stamp = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return "时刻未核实"
    return stamp.strftime("%Y-%m-%d %H:%M")


def _amount(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def render_trip_reply(reply, result):
    spec = TripReply.model_validate(reply)
    rows = result.get("tasks", [])
    reconstructed = {
        t["task_id"]: t for t in (result.get("validation") or {}).get("reconstructed_tasks", [])
    }
    requested = spec.tasks or {t["id"]: ["train", "hotel", "content"] for t in rows}
    tasks = {t["id"]: t for t in rows}
    lines = [
        "已保存以下安排："
        if result.get("status") == "completed"
        else "已保留以下安排，仍有待处理问题："
    ]
    costs = []
    shown_train = False
    for identity, parts in requested.items():
        task = tasks[identity]
        facts = reconstructed.get(identity, {})
        section = []
        train = facts.get("train") or {}
        if "train" in parts:
            shown_train = True
            price = _amount(train.get("price_cny"))
            passengers = task.get("conditions", {}).get("passengers") or 1
            costs.append(price * passengers if price is not None else None)
            if train:
                fare = f"{price}元" if price is not None else "票价未核实"
                if passengers != 1:
                    fare += f"/人，{passengers}人"
                section.append(
                    f"{train.get('train_number') or '车次未核实'}，"
                    f"{_stamp(train.get('departure_at'))}出发，"
                    f"{_stamp(train.get('arrival_at'))}抵达，"
                    f"{train.get('seat_class') or '席别未核实'}，{fare}。"
                )
            else:
                section.append("车次与票价待核实。")
        hotel = facts.get("hotel") or {}
        schedule = facts.get("schedule") or {}
        if "hotel" in parts and (hotel or task.get("requires_hotel")):
            check_in = schedule.get("check_in") or task.get("conditions", {}).get("check_in")
            check_out = schedule.get("check_out") or task.get("conditions", {}).get("check_out")
            stay = (
                f"，{check_in}入住、{check_out}退房"
                if check_in and check_out
                else "，入住退房日期待核实"
            )
            price = _amount(hotel.get("stay_total_cny"))
            quote = f"住宿报价{price}元" if price is not None else "房价与房态未核实"
            section.append(f"住宿：{hotel.get('hotel_name') or '酒店待核实'}{stay}；{quote}。")
        if "content" in parts:
            for activity in task.get("content", []):
                purpose = activity.get("purpose", "")
                description = activity.get("description", "")
                detail = f"；{description}" if description and description != purpose else ""
                section.append(
                    f"活动：{activity.get('date') or '日期待定'} "
                    f"{activity.get('time') or ''} {purpose}{detail}。"
                )
        if section:
            route = "→".join(t for t in (task.get("origin"), task.get("destination")) if t)
            lines.append((route + "：\n" if route else "") + "\n".join(section))
    if shown_train:
        known = sum((amount for amount in costs if amount is not None), Decimal(0))
        label = "交通小计" if all(amount is not None for amount in costs) else "已知交通费用"
        lines.append(
            f"{label}：{known}元（不含住宿）。"
            if any(amount is not None for amount in costs)
            else "交通费用待核实（不含住宿）。"
        )
    blocking = (result.get("validation") or {}).get("issues", [])
    if any(i.get("blocking") and i.get("code", "").startswith("policy_") for i in blocking):
        lines.append("企业政策仍有未核实事项，不能确认合规完成。")
    problems = list(
        dict.fromkeys(
            i.get("message") or "仍有事项待处理。"
            for i in blocking
            if i.get("blocking") and not i.get("code", "").startswith("policy_")
        )
    )
    if problems:
        lines.append("待处理：" + "；".join(problems[:5]))
    if spec.explanation.strip():
        lines.append(spec.explanation.strip())
    return "\n\n".join(lines)


def bind_trip_reply(result, reply):
    return {**result, "rendered_answer": render_trip_reply(reply, result)}


def stopped_trip_reply(results):
    """A stopped application turn cannot report an unexecuted prose-only plan."""
    from trip_assistant.presentation.facts import trip_results

    latest = {}
    for result in trip_results(results):
        identity = result.get("trip_plan_id")
        if identity and result.get("revision", 0) >= latest.get(identity, {}).get("revision", 0):
            latest[identity] = result
    if not latest:
        return None
    return "本轮未完成全部处理，以下是实际保存的当前安排及剩余问题。\n\n" + "\n\n".join(
        render_trip_reply({}, result) for result in latest.values()
    )


def append_related_facts(answer, results):
    """Keep independent query numbers outside the model's free-text explanation."""
    latest = {}
    for result in results:
        if result.get("name") != "query" or not isinstance(result.get("data"), list):
            continue
        for view in result["data"][: result.get("independent_count", len(result["data"]))]:
            parameters = view.get("query") or view.get("parameters") or view.get("query_id")
            key = json.dumps([view.get("domain"), parameters], sort_keys=True, ensure_ascii=False)
            latest[key] = view
    sections = []
    labels = {"weather": "天气", "hotel": "酒店候选", "train": "铁路候选", "guide": "资料"}
    fields = {
        "temp_c": "气温",
        "min_temp_c": "最低气温",
        "max_temp_c": "最高气温",
        "price_cny": "票价",
    }
    for view in latest.values():
        domain = view.get("domain")
        label = labels.get(domain, "查询")
        query = view.get("query") or view.get("parameters") or {}
        if isinstance(query, dict) and query.get("city"):
            label = query["city"] + label
        items = view.get("items", [])
        lines = []
        for item in items:
            values = [
                str(item[key])
                for key in (
                    "date",
                    "description",
                    "title",
                    "snippet",
                    "hotel_name",
                    "address",
                    "train_number",
                    "departure_at",
                    "arrival_at",
                    "seat_class",
                )
                if item.get(key) is not None
            ]
            values.extend(
                f"{label}：{item[key]}{'元' if key == 'price_cny' else '℃'}"
                for key, label in fields.items()
                if item.get(key) is not None
            )
            if item.get("url"):
                values.append(item["url"])
            if values:
                lines.append("，".join(values))
        if not lines:
            lines.append(view.get("message") or "本次查询没有可展示结果。")
        sections.append(label + "：\n" + "\n".join(lines))
    if any(r.get("name") == "write_memory" and r.get("status") == "ok" for r in results):
        sections.append("长期偏好已更新。")
    return answer + ("\n\n" + "\n\n".join(sections) if sections else "")
