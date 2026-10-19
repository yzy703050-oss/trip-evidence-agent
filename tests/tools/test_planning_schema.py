from trip_assistant.tools.registry import build_registry


def test_model_can_discover_query_conditions_and_selection_fields():
    registry = build_registry({})
    schema = registry["write_trip"].schema["function"]["parameters"]
    defs = schema["$defs"]
    query = defs["QueryItem"]["properties"]
    assert "departure_date" in query["parameters"]["properties"]
    assert query["constraints"]["properties"]["train_kind"]["enum"] == [
        "high_speed",
        "emu",
        "other",
    ]
    task = defs["TaskInput"]["properties"]
    assert "nights" in defs["PlanningConditions"]["properties"]
    selections = task["selection_refs"]
    if "$ref" in selections:
        selections = defs[selections["$ref"].split("/")[-1]]
    selection = selections["properties"]["train"]
    if "$ref" in selection:
        selection = defs[selection["$ref"].split("/")[-1]]
    refs = selection["properties"]
    assert set(refs) == {"query_id", "result_revision", "candidate_id"}


def test_memory_schema_explains_actual_update_contract():
    schema = build_registry({})["write_memory"].schema["function"]["parameters"]
    fields = schema["properties"]["updates"]["items"]["properties"]
    assert fields["op"]["enum"] == ["add", "replace", "retract"]
    assert "text" in fields and "id" in fields
