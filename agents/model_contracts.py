"""One explicit model-facing contract, backed by existing program enums."""

import json
from decimal import Decimal

from agents.contracts import FILTER_KEYS
from agents.workflow_contracts import CONDITION_KEYS, PURPOSES, SOURCES


def validate_model_money(value):
    """Only new model outputs require JSON numbers; stored decimal strings remain valid."""

    def check(amount):
        if type(amount) not in {int, float}:
            raise ValueError("model amount must be a finite nonnegative JSON number")
        decimal = Decimal(str(amount))
        if not decimal.is_finite() or decimal < 0:
            raise ValueError("model amount must be a finite nonnegative JSON number")

    if isinstance(value, dict):
        constraints = value.get("constraints")
        if isinstance(constraints, dict):
            for key, amount in constraints.items():
                if key.endswith("_cny"):
                    check(amount)
        if value.get("field") in {key for key in FILTER_KEYS if key.endswith("_cny")}:
            check(value.get("value"))
        for child in value.values():
            validate_model_money(child)
    elif isinstance(value, list):
        for child in value:
            validate_model_money(child)
    return value


def workflow_proposal_contract():
    from agents.condition_strengths import DATE_FIELDS

    strengths = {'type': 'object', 'additionalProperties': False,
                 'properties': {key: {'enum': ['hard', 'soft']} for key in sorted(DATE_FIELDS)}}
    fields = {}
    for key in sorted(CONDITION_KEYS):
        if key == "constraints":
            properties = {}
            for name in sorted(FILTER_KEYS | {"total_budget_cny"}):
                properties[name] = (
                    {"type": "number", "minimum": 0}
                    if name.endswith("_cny")
                    else {"type": "boolean"}
                    if name == "available_only"
                    else {"type": "string"}
                )
            fields[key] = {
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
            }
        elif key in {"passengers", "guests", "nights"}:
            fields[key] = {"type": ["integer", "null"], "minimum": 1}
        elif key in {"hotel_quote_required", "flexible_dates"}:
            fields[key] = {"type": ["boolean", "null"]}
        else:
            fields[key] = {"type": ["string", "null"]}
    conditions = {"type": "object", "additionalProperties": False, "properties": fields}
    task = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "origin": {"type": ["string", "null"]},
            "destination": {"type": "string"},
            "purpose": {"enum": sorted(PURPOSES)},
            "purpose_source": {"enum": sorted(SOURCES)},
            "requires_hotel": {"type": "boolean"},
            "conditions": {"$ref": "#/$defs/conditions"},
            "field_sources": {"type": "object", "additionalProperties": {"enum": sorted(SOURCES)}},
            "condition_strengths": strengths,
        },
        "required": ["origin", "destination", "purpose", "requires_hotel", "conditions"],
    }
    schema = {
        "type": "object",
        "additionalProperties": False,
        "$defs": {"conditions": conditions},
        "properties": {
            "confirmed_conditions": {"$ref": "#/$defs/conditions"},
            "condition_strengths": strengths,
            "tasks": {"type": "array", "minItems": 1, "items": task},
        },
        "required": ["tasks"],
    }
    example = {
        "confirmed_conditions": {
            "passengers": 1,
            "guests": 1,
            "constraints": {"seat_class": "二等座", "train_max_total_cny": 700},
        },
        "tasks": [
            {
                "origin": "重庆",
                "destination": "上海",
                "purpose": "visit",
                "purpose_source": "user",
                "requires_hotel": True,
                "conditions": {
                    "departure_date": "2026-10-15",
                    "check_in": "2026-10-15",
                    "check_out": "2026-10-17",
                    "hotel_keywords": "全季",
                },
                "field_sources": {
                    "departure_date": "user",
                    "check_in": "user",
                    "check_out": "user",
                    "hotel_keywords": "preference",
                },
                "condition_strengths": {"departure_date": "hard", "check_in": "hard", "check_out": "hard"},
            },
            {
                "origin": "上海",
                "destination": "重庆",
                "purpose": "return",
                "purpose_source": "user",
                "requires_hotel": False,
                "conditions": {"departure_date": "2026-10-17"},
                "field_sources": {"departure_date": "user"},
                "condition_strengths": {"departure_date": "hard"},
            },
        ],
    }
    return {"schema": schema, "example": example}


def workflow_contract_prompt():
    return (
        "严格按以下schema生成提案。example只展示类型与层级，不复制其中城市、日期、品牌或金额。"
        "旅游purpose用visit，出差business，返程return，绝不写tourism。"
        "hotel_keywords是单个字符串（如全季），不是列表；无关键词可省略。"
        "purpose只在task层；seat_class和金额只在constraints内。"
        "condition_strengths与field_sources独立：来源user不等于强制；日期及nights标hard或soft。"
        "必须/只能/不能晚于以及未表示可调整的明确日期用hard；最好/尽量/可调整和系统建议用soft。"
        "全局condition_strengths对应confirmed_conditions，任务同名字段对应任务条件；"
        "任务不能软化全局硬边界，政策与预算仍强制；不输出没有条件值的强度。"
        "单程预算不能改为全程预算：每个单程700用train_max_total_cny=700；"
        "只有明确全程总预算才用total_budget_cny。"
        "新workflow的基本火车/酒店查询由程序从任务条件生成，不要另输出query_plan；"
        "只有独立查询或明确的额外天气查询/查询依赖才输出query_plan。"
        "query_plan.parameters只含工具参数，不能放purpose/hotel_quote_required/hotel_brands，"
        "席别/金额放parameters.constraints，酒店品牌放parameters.keywords。\n"
        "结构契约JSON："
        + json.dumps(workflow_proposal_contract(), ensure_ascii=False, separators=(",", ":"))
        + "\n"
    )


def batch_query_contract_prompt():
    return (
        "query_tools提供实际工具schema与能力；只用允许的parameters键，不能发明报价接口。"
        "本轮不为接口缺字段、失败或报价超标继续补查；已有候选先选择，无法满足则说明已查范围。"
        "只有合法新日期安排或用户本轮明确新查询才有新参数依据；schedule_queries用query_plan，"
        "task_id/task_revision取当前值，绝不是queries。"
        "酒店品牌参数叫keywords，不叫hotel_keywords；预算放parameters.constraints.hotel_max_nightly_cny，"
        "不能放max_nightly_cny/quote_required/search_kind。分页用parameters.candidate_offset，不能顶层offset。"
        "task_results.items已经通过该query.constraints本地筛选；有合适候选优先commit_batch，"
        "不能把未提供的字段推测为可以查到，也不能把当前候选当成全市场。"
        "returned_candidate_total是原返回数，excluded_candidate_count是筛选排除数。"
        "stay_total_cny是整个住宿总额，绝不能直接与每晚上限比较：760元住两晚是380元/晚。"
        "酒店derived_stay_nights/derived_nightly_cny由程序按报价日期计算，可用于比较每晚预算；"
        "仍须核对fees_included、日期与可用性，缺字段不假装有报价。金额约束输出JSON数字。"
        "酒店仅有地点能力时不能补查报价；已失败且无新依据的火车查询也不反复重试。"
        "缺候选时整个train_selection或hotel_selection必须为null，不能给candidate_id=null的引用对象。"
        "能力缺口直接commit_batch附allow_repair=false、已有选择及具体unverified_requirements；"
        "程序校验决定completed或partial，软偏好未核实不能伪装成政策满足。"
        "仅成功查询无候选或真实日期衔接问题且相关日期为soft时，才考虑合理调整去程与返程。"
        "用户明确要求无车时改期、且新安排在hard边界内时，应提出日期补丁；soft授权构成新参数依据，"
        "不是已经有新日期票房的保证，也不需要先有新日期的查询结果。"
        "repair_tasks需要workflow_revision、task_ids、components=[schedule]、实际conflict_codes、reason，"
        "task_updates=[{task_id,conditions:{departure_date:新日期,check_in:日期,check_out:日期}}]，"
        "每条只写需修改的soft日期/nights；固定hard条件不得改变。程序只查询新日期受影响组件，"
        "不能改旧报价日期冒充新事实，不能靠不断改日期补齐接口缺字段。"
        "无法兼容的hard条件反馈用户可放宽项，证据未知只说明查不到，不用授权代替事实。\n"
    )
