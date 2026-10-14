"""Literal user targets and displayed evidence authorize saved-plan selections."""

import re
from copy import deepcopy

from agents.workflow_queries import resolve_selection


def validate_selection_requirements(value, original_query):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {"hotel", "train"}:
        raise ValueError("invalid selection requirements")
    checked = {}
    for domain, requirement in value.items():
        if not isinstance(requirement, dict) or set(requirement) != {"name_contains"}:
            raise ValueError("selection target needs a literal name_contains")
        name = requirement["name_contains"]
        if not isinstance(name, str) or not name.strip() or name not in original_query:
            raise ValueError("selection target is not supported by current user text")
        checked[domain] = {"name_contains": name.strip()}
    return checked


def resolve_literal_selection_requirements(original_query, candidate_rows, proposed=None):
    requirements = validate_selection_requirements(proposed, original_query)
    if "hotel" in requirements:
        return requirements
    if not any(row.get("domain") == "hotel" for row in candidate_rows):
        return requirements
    patterns = (
        r"名称(?:带|包含|含有|含)\s*[“\"\']?(.+?)(?:的那家|的那|的酒店|的地点|[”\"\'，,。；;\n]|$)",
        r"(?:换成|改为|改成|替换为)\s*[“\"\'](.+?)[”\"\']",
        r"(?:酒店|住宿)[^，,。；;\n]{0,12}?(?:换成|改为|改成|替换为)\s*(.+?)(?:[，,。；;\n]|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, original_query)
        if match:
            literal = match[1].strip()
            # Descriptive phrases are not candidate names. A later explicit
            # "名称带" clause is handled by the first pattern.
            if (
                literal
                and len(literal) <= 256
                and not any(
                    word in literal for word in ("候选", "一家", "其他", "另一", "合适", "便宜")
                )
            ):
                requirements["hotel"] = {"name_contains": literal}
                return requirements
    names = {
        item.get("hotel_name")
        for row in candidate_rows
        if row.get("domain") == "hotel"
        for item in row.get("items", [])
        if item.get("hotel_name") and item["hotel_name"] in original_query
    }
    if len(names) == 1:
        requirements["hotel"] = {"name_contains": names.pop()}
    return requirements


def matches_selection_requirement(item, domain, requirement):
    field = "hotel_name" if domain == "hotel" else "train_number"
    return requirement["name_contains"].casefold() in str(item.get(field, "")).casefold()


def validate_amendment_selection(patch, requirements, shown_refs, workflow):
    for domain, ref in patch.get("selection_refs", {}).items():
        if ref is None:
            if domain in requirements:
                raise ValueError("required selection target is missing")
            continue
        key = (ref.get("query_id"), ref.get("result_revision"), ref.get("candidate_id"))
        if key not in shown_refs:
            raise ValueError(
                "candidate reference was not shown; read cached evidence before selecting"
            )
        item = resolve_selection(workflow, patch["task_id"], patch["expected_task_revision"], ref)
        if domain in requirements and not matches_selection_requirement(
            item, domain, requirements[domain]
        ):
            raise ValueError("selected candidate does not match the requested literal target")
    if set(requirements) - set(patch.get("selection_refs", {})):
        raise ValueError("required selection target is missing")
    return deepcopy(patch)
