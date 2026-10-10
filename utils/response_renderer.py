"""Build the complete final_answer from validated facts without another model call."""

import ast
import json
import re

from travel_data.result_guard import grounded_answer, guard_domain_result


FIELDS = {
    "origin": "出发城市",
    "destination": "目的地",
    "city": "城市",
    "dates": "行程日期",
    "departure_date": "出发日期",
    "arrival_date": "抵达日期",
    "arrival_before": "最晚抵达时间",
    "start_date": "开始日期",
    "end_date": "结束日期",
    "check_in": "入住日期",
    "check_out": "离店日期",
    "passengers": "乘车人数",
    "guests": "入住人数",
    "nights": "住宿晚数",
    "train": "火车",
    "hotel": "酒店",
    "other": "其他费用",
    "unverified": "尚未核实的项目",
    "seat_class": "席别",
    "hotel_keywords": "酒店要求",
    "home_location": "长期居住城市",
    "hotel_brands": "酒店品牌偏好",
    "seat_preference": "席别偏好",
    "airlines": "航空公司偏好",
}
DOMAINS = {"train": "火车", "hotel": "酒店", "guide": "攻略", "weather": "天气", "web": "网页"}
STATES = {
    "ok": "查询完成",
    "completed": "安排已完成",
    "partial": "部分完成",
    "needs_input": "需要补充条件",
    "unavailable": "服务暂时不可用",
    "error": "查询失败",
    "available": "查询时有票或空房",
    "unknown": "尚未核实",
    "verified": "已核实",
    "needs_check": "待核实",
}
DOMAIN_STATES = {
    "ok": "已完成",
    "partial": "仅部分完成",
    "needs_input": "需要补充条件",
    "unavailable": "服务暂时不可用",
    "error": "失败",
}
STOP_REASONS = {
    "no_progress": "继续查询没有取得新进展，已停止本轮处理，已有结果已保留",
    "main_decision_limit": "本轮决策次数已达到上限，已有草稿已保留",
    "task_information_limit": "当前段资料查询已达到上限，已有结果已保留",
    "workflow_tool_limit": "本轮工具查询已达到上限，已有结果已保留",
    "turn_timeout": "本轮处理超时，已有结果已保留",
    "conditions_unresolved": "仍有条件或冲突尚未解决，可以继续补充或修改",
    "invalid_workflow_action": "本轮处理遇到无效响应，已停止继续查询",
    "workflow_save_failed": "行程状态保存失败，已停止处理，请稍后重试",
    "feedback_limit": "本轮补查已达到上限，已有结果已保留",
    "information_limit": "本轮信息获取已达到上限，已有结果已保留",
    "invalid_feedback": "仍缺少可执行的补查条件，可以继续补充或修改",
}


def _prose(value) -> str:
    """Unwrap response text; never stringify arbitrary structured objects."""
    if isinstance(value, dict):
        answer = next(
            (
                text
                for key in ("final_answer", "answer", "summary", "message")
                if (text := _prose(value.get(key)))
            ),
            "",
        )
        if answer:
            return answer
        if isinstance(value.get("preferences"), dict):
            return _prose(value["preferences"])
        return "，".join(
            f"{FIELDS[key]}为{text}"
            for key in ("home_location", "hotel_brands", "seat_preference", "airlines")
            if (text := _prose(value.get(key)))
        )
    if isinstance(value, list):
        return "、".join(filter(None, (_prose(item) for item in value)))
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip() if lines[-1].strip() == "```" else ""
    if text.startswith(("{", "[")):
        try:
            return _prose(json.loads(text))
        except (ValueError, TypeError):
            try:
                return _prose(ast.literal_eval(text))
            except (ValueError, SyntaxError):
                return ""
    # Grounded summaries can include standalone machine statuses; URLs stay intact.
    pattern = r"(?<![A-Za-z0-9_/:.\-])(" + "|".join(STATES) + r")(?![A-Za-z0-9_/\-.])"
    # Parse whole nested JSON values, rather than replacing their innermost braces.
    # Normal Markdown labels are not JSON and must keep their link text.
    chunks, cursor = [], 0
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\{\[]", text):
        start = match.start()
        if start < cursor:
            continue
        try:
            structured, end = decoder.raw_decode(text, start)
        except ValueError:
            literal = re.match(r"\{[^{}]*\}|\[[^\[\]]*\]", text[start:])
            if not literal:
                continue
            try:
                structured = ast.literal_eval(literal[0])
            except (ValueError, SyntaxError):
                continue
            end = start + literal.end()
        chunks.extend([text[cursor:start], _prose(structured)])
        cursor = end
    text = "".join(chunks) + text[cursor:]
    return re.sub(pattern, lambda match: STATES[match[1]], " ".join(text.split()))


def _sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text.endswith(("。", "！", "？", "!", "?")) else text + "。"


def _source(source, fetched_at=None) -> str:
    source = source if isinstance(source, dict) else {}
    parts = []
    if source.get("provider"):
        parts.append(f"来源为{source['provider']}")
    if source.get("url"):
        parts.append(f"参考链接为 {source['url']}")
    timestamp = fetched_at or source.get("fetched_at")
    if timestamp:
        parts.append(f"查询时间为 {timestamp}")
    return "，".join(parts)


def _missing(fields) -> str:
    if not isinstance(fields, list) or not fields:
        return ""
    names = list(
        dict.fromkeys(FIELDS.get(field, "相关条件") for field in fields if isinstance(field, str))
    )
    return "请补充" + "、".join(names) if names else ""


def _offer(item: dict, kind: str) -> list[str]:
    parts = []
    if kind == "train":
        route = "到".join(
            item.get(key) or "未确认车站" for key in ("origin_station", "destination_station")
        )
        text = f"可参考{item.get('train_number') or '该车次'}，从{route}"
        if item.get("departure_at") or item.get("departure_time"):
            text += f"，{item.get('departure_at') or item['departure_time']}出发"
        if item.get("arrival_at") or item.get("arrival_time"):
            text += f"，{item.get('arrival_at') or item['arrival_time']}抵达"
        if item.get("seat_class"):
            text += f"，{item['seat_class']}"
        text += (
            f"单人票价为{item['price_cny']}元"
            if item.get("price_cny") is not None
            else "票价尚未核实"
        )
        if item.get("availability") == "available":
            text += "，查询时有票"
        elif item.get("availability") == "unavailable":
            text += "，查询时无票"
        else:
            text += "，余票尚未核实"
        if item.get("remaining") is not None:
            text += f"，余票{item['remaining']}张"
        parts.append(text)
    elif item.get("kind") == "hotel_place":
        text = f"可参考{item.get('hotel_name') or '该酒店'}"
        if item.get("address"):
            text += f"，位于{item['address']}"
        parts.append(text + "；这是酒店地点参考，房价、空房和入住规则尚未核实")
    else:
        text = f"住宿可参考{item.get('hotel_name') or '该酒店'}"
        if item.get("room_type"):
            text += f"的{item['room_type']}"
        if item.get("check_in"):
            text += f"，{item['check_in']}入住"
        if item.get("check_out"):
            text += f"，{item['check_out']}离店"
        if item.get("guests"):
            text += f"，{item['guests']}人入住"
        text += (
            f"，住宿总价为{item['stay_total_cny']}元"
            if item.get("stay_total_cny") is not None
            else "，住宿总价尚未核实"
        )
        text += (
            "（含费用）"
            if item.get("fees_included") is True
            else "（未包含全部费用）"
            if item.get("fees_included") is False
            else "，费用包含范围尚未核实"
        )
        text += (
            "，查询时有空房"
            if item.get("availability") == "available"
            else "，查询时无空房"
            if item.get("availability") == "unavailable"
            else "，空房尚未核实"
        )
        text += (
            f"，退改规则：{item['cancellation']}"
            if item.get("cancellation")
            else "，退改规则尚未核实"
        )
        parts.append(text)
    if item.get("needs_revalidation"):
        parts.append("上述价格和库存是此前取得的信息，需要重新核实")
    parts.append(_source(item.get("source")))
    return parts


def _domain(domain: str, raw: dict) -> list[str]:
    data = guard_domain_result(domain, raw)
    status = data.get("status", "error")
    parts = [
        f"{DOMAINS[domain]}查询{DOMAIN_STATES[status]}",
        _prose(data.get("message")),
        _missing(data.get("missing_fields")),
    ]
    if status in {"ok", "partial"}:
        items = data.get("items", [])
        if not items:
            parts.append(f"未取得可核实的{DOMAINS[domain]}候选结果")
            if domain in {"train", "hotel"}:
                parts.append("无法确认票务或房间库存")
        for item in items:
            if domain in {"train", "hotel"}:
                parts.extend(_offer(item, domain))
            elif domain == "guide":
                parts.append(
                    f"{item['content']}（{'已核实' if item['verification'] == 'verified' else '待核实'}）"
                )
                parts.append(_source(item.get("source")))
            elif domain == "weather":
                text = f"{data.get('query', {}).get('city', '')}{item.get('date') or '当前'}的天气为{item.get('description') or '天气描述未提供'}"
                if item.get("min_temp_c") is not None and item.get("max_temp_c") is not None:
                    text += f"，气温为{item['min_temp_c']}～{item['max_temp_c']}℃"
                elif item.get("temp_c") is not None:
                    text += f"，气温为{item['temp_c']}℃"
                parts.extend([text, _source(item.get("source"))])
            else:
                parts.extend(
                    [
                        f"{item.get('title') or '网页资料'}：{item.get('snippet') or '未提供摘要'}，参考链接为 {item['url']}",
                        _source(item.get("source")),
                    ]
                )
    parts.append(_source(data.get("source"), data.get("fetched_at")))
    return parts


def _budget(budget: dict) -> list[str]:
    if not budget:
        return []
    parts = []
    if budget.get("lines") and budget.get("known_subtotal_cny") is not None:
        parts.append(f"已知费用小计为{budget['known_subtotal_cny']}元")
    else:
        parts.append("目前尚无可核实的费用报价")
    missing = budget.get("missing_categories", [])
    categories = [item.get("category") if isinstance(item, dict) else item for item in missing]
    if categories:
        names = list(dict.fromkeys(FIELDS.get(key, "其他项目") for key in categories))
        parts.append("未报价的项目包括" + "、".join(names))
    parts.append(
        "全程费用已经核实"
        if budget.get("verified", budget.get("complete")) is True
        else "全程费用尚未完整核实"
    )
    return parts


def build_final_answer(result: dict) -> str:
    """Return a single paragraph; preserve the internal result and all fact guards."""
    domains = result.get("domain_results") or {}
    answer = result.get("final_answer")
    if not (
        domains
        or result.get("workflow")
        or result.get("itinerary")
        or result.get("missing_fields")
        or result.get("status") in {"error", "partial"}
    ):
        # Direct model replies are already complete; keep their exact prose.
        return _prose(answer) or "本轮暂未取得可展示的结果，请补充需求或稍后重试。"
    if domains and isinstance(answer, str):
        # Harness already produces these grounded lines. Rebuild them once below,
        # retaining other lines such as preference confirmations or memory answers.
        grounded_lines = {
            _prose(line) for line in grounded_answer({"domain_results": domains}).splitlines()
        }
        answer = "\n".join(
            line for line in answer.splitlines() if _prose(line) not in grounded_lines
        )
    parts = [_prose(answer), _missing(result.get("missing_fields"))]
    if result.get("workflow"):
        checked = result.get("validated_plan") or {}
        for task in checked.get("reconstructed_tasks", []):
            parts.append(
                f"从{task.get('origin') or '待确认的出发城市'}到{task.get('destination') or '待确认的目的地'}"
            )
            schedule = task.get("schedule") or {}
            for field, label in (
                ("departure_at", "出发时间"),
                ("arrival_at", "抵达时间"),
                ("check_in", "入住日期"),
                ("check_out", "离店日期"),
            ):
                if schedule.get(field):
                    parts.append(f"{label}为{schedule[field]}")
            for kind in ("train", "hotel"):
                if task.get(kind):
                    parts.extend(_offer(task[kind], kind))
        parts.extend(_budget(checked.get("budget", {})))
        for gap in result.get("gaps", []):
            parts.append(_prose(gap.get("message") if isinstance(gap, dict) else gap))
    else:
        for domain, raw in domains.items():
            if domain in DOMAINS and isinstance(raw, dict):
                parts.extend(_domain(domain, raw))
        if result.get("itinerary"):
            # Legacy support needs the agent guard only after module initialization.
            from agents.itinerary_module import guard_final_itinerary

            checked = guard_final_itinerary(
                result["itinerary"], domains, result.get("travel_conditions")
            )
            for day in checked["itinerary"].get("daily_plans", []):
                label = day.get("date") or (f"第{day['day']}天" if day.get("day") else "行程当天")
                activities = [
                    " ".join(filter(None, (item.get("time"), item.get("location"))))
                    for item in day.get("activities", [])
                ]
                parts.append(
                    f"{label}可安排" + "、".join(activities)
                    if activities
                    else f"{label}的活动尚待安排"
                )
            for kind in ("train", "hotel"):
                if checked.get(f"selected_{kind}"):
                    parts.extend(_offer(checked[f"selected_{kind}"], kind))
            parts.extend(_budget(checked.get("budget", {})))
            parts.append(_missing(checked.get("missing_fields")))
    if result.get("status") == "error":
        parts.append("本轮未能完成请求，请稍后重试或补充条件")
    elif result.get("status") == "partial":
        parts.append("本轮仅完成部分内容，仍有事项待核实")
    if result.get("stop_reason") in STOP_REASONS:
        parts.append(STOP_REASONS[result["stop_reason"]])
    sentences = list(dict.fromkeys(_sentence(part) for part in parts if part))
    return " ".join(sentences) or "本轮暂未取得可展示的结果，请补充需求或稍后重试。"


def finalize_business_result(result: dict) -> dict:
    """Keep the JSON envelope; make its final_answer the full public reply."""
    return {**result, "final_answer": build_final_answer(result)}
