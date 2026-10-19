"""Public dictionary fields shared by tool descriptions and whitelist validation."""


def object_schema(properties):
    return {"type": "object", "properties": properties, "additionalProperties": False}


TEXT = {"type": "string"}
DAY = {"type": "string", "description": "YYYY-MM-DD"}
STAMP = {"type": "string", "format": "date-time"}
COUNT = {"type": "integer", "minimum": 1}
FLAG = {"type": "boolean"}
MONEY = {"type": "number", "minimum": 0}
PARAMETERS = {
    "train": dict(
        origin=TEXT,
        destination=TEXT,
        departure_date=DAY,
        start_date=DAY,
        arrival_date=DAY,
        arrival_before=STAMP,
        passengers=COUNT,
    ),
    "hotel": dict(
        city=TEXT,
        destination=TEXT,
        check_in=DAY,
        check_out=DAY,
        guests=COUNT,
        keywords=TEXT,
        search_kind=TEXT,
    ),
    "weather": dict(city=TEXT, date={**DAY, "description": "预报日期；查询现在实况时省略date"}),
    "web": dict(query=TEXT),
}
QUERY_PARAMETERS = object_schema(
    {k: v for fields in PARAMETERS.values() for k, v in fields.items()}
)
QUERY_PARAMETERS["description"] = (
    "按domain仅使用对应字段："
    + "；".join(domain + ":" + ",".join(fields) for domain, fields in PARAMETERS.items())
    + "。train用departure_date，不用date/from/to；席别、车种和预算放constraints。"
    "TripPlan items.parameters可为空，由task.conditions继承。"
)
FILTERS = object_schema(
    dict(
        hotel_max_total_cny=MONEY,
        hotel_max_nightly_cny=MONEY,
        train_max_total_cny=MONEY,
        seat_class=TEXT,
        train_kind={"type": "string", "enum": ["high_speed", "emu", "other"]},
        departure_time_after=TEXT,
        departure_time_before=TEXT,
        available_only=FLAG,
    )
)
CONDITIONS = object_schema(
    dict(
        start_date=DAY,
        end_date=DAY,
        departure_date=DAY,
        arrival_date=DAY,
        arrival_before=STAMP,
        check_in=DAY,
        check_out=DAY,
        nights=COUNT,
        passengers=COUNT,
        guests=COUNT,
        hotel_keywords=TEXT,
        flexible_dates=FLAG,
        hotel_quote_required=FLAG,
        constraints=object_schema({**FILTERS["properties"], "total_budget_cny": MONEY}),
    )
)
REF = {
    **object_schema(dict(query_id=TEXT, result_revision=COUNT, candidate_id=TEXT)),
    "required": ["query_id", "result_revision", "candidate_id"],
}
SELECTIONS = object_schema(dict(train=REF, hotel=REF))
SELECTIONS["description"] = "仅select/amend引用该任务已展示候选；propose不要填写。"
MEMORY_UPDATE = object_schema(
    dict(
        op={"type": "string", "enum": ["add", "replace", "retract"]},
        text=TEXT,
        id=TEXT,
    )
)
MEMORY_UPDATE.update(required=["op"], description="add需text；replace需id/text；retract需已有id。")
