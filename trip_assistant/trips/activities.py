"""Task activities: stable identities and guarded edits, separate from supplier facts."""

import re
from copy import deepcopy
from datetime import datetime, timedelta
from uuid import uuid4

_ACTIVITY_TERMS = (("开会", "会议"), ("讲座",), ("培训",), ("拜访",), ("会面",))


def apply_content(current, proposed):
    from trip_assistant.trips.contracts import ActivityInput

    known = {a["id"]: a for a in current}
    result, seen = [], set()
    for raw in proposed:
        item = ActivityInput.model_validate(raw).model_dump(exclude_none=True)
        if item["source"] in {"preference", "default", "proposal"}:
            if item["strength"] != "soft":
                raise ValueError("model or memory activity proposals must be soft")
            item["source"] = "proposal"
        identity = item.get("id")
        if identity and (identity not in known or identity in seen):
            raise ValueError("activity identity is unknown or repeated")
        if identity:
            previous = known[identity]
            if previous.get("strength") == "hard" and item["strength"] != "hard":
                raise ValueError("cannot soften hard activity")
            old_time, new_time = previous.get("time"), item.get("time")
            if old_time and new_time and old_time != new_time:
                for key in ("purpose", "description"):
                    if item[key] == previous[key]:
                        item[key] = item[key].replace(old_time, new_time)
        else:
            item["id"] = "activity_" + uuid4().hex
        seen.add(item["id"])
        result.append(item)
    return deepcopy(result)


def bind_user_times(content, *, query, departure_date=None, current_time=None, enabled=True):
    """Preserve explicitly stated coarse meeting times, without inventing precision."""
    if not enabled:
        return content
    if re.search(r"改(?:成|为|到)|取消|删除", query):
        return content
    for relative, period, phrase in re.findall(
        r"(今天|明天|后天)?(上午|下午|晚上)([^，。；\n]{0,18})", query
    ):
        if re.search(r"不|取消|别", phrase):
            continue
        expected = departure_date
        if relative and current_time:
            today = datetime.fromisoformat(current_time).date()
            expected = (
                today + timedelta(days={"今天": 0, "明天": 1, "后天": 2}[relative])
            ).isoformat()
        for item in content:
            text = item["purpose"] + item["description"]
            if item["source"] != "user" or not any(
                any(word in phrase for word in group) and any(word in text for word in group)
                for group in _ACTIVITY_TERMS
            ):
                continue
            if expected and item.get("date") and item["date"] != expected:
                raise ValueError("user activity date must remain " + expected)
            if expected:
                item["date"] = expected
            item["time"] = period
    return content


def activity_issues(task, plan, next_departure=None):
    """Check only determinable travel windows; vague text never invents exact times."""
    issues = []
    arrival = plan.get("schedule", {}).get("arrival_at")
    for item in task.get("content", []):
        day = item.get("date")
        if not day:
            continue
        time = item.get("time") or ""
        minute = None
        exact = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", time)
        if exact:
            minute = int(exact[1]) * 60 + int(exact[2])
        end = {"上午": 12 * 60, "下午": 18 * 60, "晚上": 24 * 60}.get(time, minute)
        start = {"上午": 0, "下午": 12 * 60, "晚上": 18 * 60}.get(time, minute)
        code = None
        crosses_boundary = False
        if arrival:
            stamp = datetime.fromisoformat(arrival)
            arrival_minute = stamp.hour * 60 + stamp.minute + stamp.second / 60
            if day < stamp.date().isoformat() or (
                day == stamp.date().isoformat()
                and end is not None
                and (arrival_minute > end if exact else arrival_minute >= end)
            ):
                code = "activity_before_arrival"
            elif day == stamp.date().isoformat() and start is not None and not exact:
                crosses_boundary = start < arrival_minute < end
        if next_departure:
            stamp = datetime.fromisoformat(next_departure)
            departure_minute = stamp.hour * 60 + stamp.minute + stamp.second / 60
            if day > stamp.date().isoformat() or (
                day == stamp.date().isoformat() and start is not None and start >= departure_minute
            ):
                code = "activity_after_departure"
            elif day == stamp.date().isoformat() and start is not None and not exact:
                crosses_boundary |= start < departure_minute < end
        if code is None and crosses_boundary:
            code = "activity_window_unverified"
        if code:
            destination = task.get("destination")
            issues.append(
                {
                    "code": code,
                    "task_ids": [task["id"]],
                    "blocking": code != "activity_window_unverified",
                    "message": "content.time的粗时段与实际交通部分重叠，尚未完全核实；"
                    "只改description不会改变time的区间解释。请结合抵达后/离开前的安排判断，"
                    "合理安排无需为通过校验虚构精确时刻，保留用户硬活动原要求。"
                    if code == "activity_window_unverified"
                    else "活动时段与实际交通不衔接。"
                    f"content是抵达目的地{destination or ''}后的活动，"
                    "不记录该段乘车或出发前退房。软活动可修改或删除，硬活动须保留并调整允许的软交通。",
                    "evidence": item.get("id"),
                    "activity": deepcopy(item),
                    "activity_scope": {"destination": destination, "kind": "destination_activity"},
                    "repair_permissions": {
                        "automatic_activity_edit": item.get("strength") != "hard"
                    },
                    "travel_window": {"arrival_at": arrival, "departure_at": next_departure},
                    **(
                        {
                            "coarse_window": {
                                "start": f"{start // 60:02d}:{start % 60:02d}",
                                "end": f"{end // 60:02d}:{end % 60:02d}",
                            }
                        }
                        if code == "activity_window_unverified"
                        else {}
                    ),
                }
            )
    return issues
