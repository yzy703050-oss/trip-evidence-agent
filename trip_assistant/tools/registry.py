"""Actual native tool schemas and their matching validators."""

import json
from dataclasses import dataclass, replace
from typing import ClassVar, Literal

from pydantic import Field, model_validator

from trip_assistant.presentation.trip_reply import TripReply
from trip_assistant.tools.schema import compact_schema
from trip_assistant.tools.trip_contracts import (
    NativePlan,
    PrePolicyPlan,
    ReviewPlan,
    SinglePassPlan,
)
from trip_assistant.trips.contracts import GoalAssessment, Input, QueryItem, TripPlanInput
from trip_assistant.trips.parameter_schema import CONDITIONS, MEMORY_UPDATE

TRIP_ARGUMENT_HELP = (
    "规划同批request_kind统一planning。propose同交全部tasks/items，不填ID、版本、selection_refs或reply；"
    "每task一段城际旅程，住宿归抵达段，content只存目的地活动，返程通常为空。"
    "default_round_trip默认true，明确单程才false。confirmed_conditions放人数/预算/最晚返回，不填confirmed_conditions.start_date；"
    "日期放task.conditions，建议标proposal/soft；旧全局hard边界不可软化。"
    "活动date/time须显式，time用上午/下午/晚上或HH:MM，跨时段拆分。"
    "默认train查询constraints.train_kind=high_speed；arrival_before只接受用户精确期限、ISO含时区。"
    "soft不筛选；同日会后返程的初始item用ranking=[{field:departure_at,direction:desc}]看较晚车次；"
    "可重排缓存，不重复soft查询或升级hard。"
    '元数据按叶键如field_sources={"train_kind":"proposal"}，condition_strengths只用hard/soft。'
    "select只交计划身份/版本及task_id/selection_refs，candidate_id复制对应items.id，不推算时刻或ID。"
    "amend只交变化和必要依赖；只改活动交task_id/components=[content]/content，保留真实活动id，新增不填id；"
    "删活动交保留后的完整列表，可空。交通/住宿条件变化须同交领域items，仅conditions/components不查新结果；"
    "独立补丁合并tasks，不带变更领域旧引用，查询后再选。QUERY_REBIND_REQUIRED先补当前条件item；"
    "selection_saved=false按新版本候选重选。"
    '最终select/amend同交reply={}展示保存全貌，局部reply.tasks={任务ID:["content"]}；route_tasks变动用全貌。'
    "reply.explanation限无数字取舍/缺口，不另述车次、时间或活动。"
    "round_summary记变化原因，不用final_answer。"
)


class Arguments(Input):
    request_kind: Literal["lookup", "planning"]


class ReadArguments(Arguments):
    resource: Literal["trip_summaries", "trip", "session_summaries", "session"]
    session_id: str | None = None
    trip_plan_id: str | None = None
    offset: int = Field(default=0, ge=0)
    filters: dict = Field(default_factory=dict)


class QueryArguments(Arguments):
    query_plan: list[QueryItem] = Field(
        min_length=1,
        max_length=12,
        description="仅独立外部查询；TripPlan待查item由状态机自动执行，不在此重复提交。",
    )


class MemoryArguments(Arguments):
    operation: Literal["update", "reanalyze"] = "update"
    updates: list[dict] = Field(
        default_factory=list,
        json_schema_extra={
            "items": MEMORY_UPDATE,
        },
    )

    @model_validator(mode="after")
    def valid_operation(self):
        if self.operation == "update" and not self.updates:
            raise ValueError("memory update needs changes")
        if self.operation == "reanalyze" and self.updates:
            raise ValueError("reanalysis cannot include explicit updates")
        return self


class TripArguments(Arguments):
    tripplan: NativePlan
    default_operation: ClassVar[str] = "propose"

    @model_validator(mode="before")
    @classmethod
    def default_plan_operation(cls, value):
        if isinstance(value, dict) and isinstance(value.get("tripplan"), dict):
            value = {**value, "tripplan": {"operation": cls.default_operation, **value["tripplan"]}}
        return value

    @model_validator(mode="after")
    def canonical_validation(self):
        TripPlanInput.model_validate(self.tripplan.model_dump(exclude_unset=True))
        return self

    reply: TripReply | None = None

    round_summary: str | None = Field(
        default=None,
        max_length=300,
        description="一句中文记录本轮判断、选择或调整原因，供下轮评估。",
    )
    final_answer: str | None = Field(
        default=None,
        description="选择/修改时同次写自然中文答复，按已知交通住宿活动介绍，不固定列未知项或追问；实际保存后展示，勿虚构完成/合规或展示内部ID。",
    )
    goal_assessment: GoalAssessment | None = Field(
        default=None,
        description="正式选择/修改同次评估原query目标；替代方案不等于满足。待查询可省略。",
    )


class ReviewTripArguments(TripArguments):
    tripplan: ReviewPlan
    default_operation: ClassVar[str] = "select"


class PrePolicyTripArguments(TripArguments):
    tripplan: PrePolicyPlan


class SinglePassTripArguments(TripArguments):
    tripplan: SinglePassPlan
    final_answer: str = Field(min_length=1)


class SinglePassMemoryArguments(MemoryArguments):
    operation: Literal["update"] = "update"


def single_pass_registry(registry):
    """Offer only actions which do not delegate another model decision."""
    result = {
        k: v for k, v in registry.items() if k in {"read", "query", "write_trip", "write_memory"}
    }
    if "write_trip" in result:
        result["write_trip"] = replace(
            result["write_trip"],
            arguments=SinglePassTripArguments,
            description="一次提交完整行程草案或修改及final_answer；reason说明铁路耗时估算依据。"
            "未知车次不填选择引用，查询结果由程序另存证据，本轮不再调模型选择。"
            "估算不能冒充车次票价库存；已有有效引用仍须校验。",
        )
    if "write_memory" in result:
        result["write_memory"] = replace(
            result["write_memory"],
            arguments=SinglePassMemoryArguments,
            description="同批增加、修改、撤回用户明确的长期偏好；不调用模型重分析。",
        )
    return result


def creation_registry(registry, *, policy_observed):
    result = dict(registry)
    if (
        not policy_observed
        and "write_trip" in result
        and "query_policy" in result
        and issubclass(result["write_trip"].arguments, TripArguments)
    ):
        result["write_trip"] = replace(
            result["write_trip"],
            arguments=PrePolicyTripArguments,
            description="当前仅可新建无需新政策的旅游等行程；business尚不可创建。"
            "用户出差须先单独query_policy，观察返回后才开放business。"
            "不要将出差改标旅游；政策查询可与独立读查同批，不能与write_trip同批。"
            "首次propose不带reply，观察候选后才在select/amend提交绑定回复。"
            "原有行程按ID修改；select只选引用，内容变化用amend。"
            "arrival_before仅用于用户明确给出的精确截止时间，须完整ISO日期时间；"
            "上午会议只记content.time=上午，不能推成09:00硬期限。",
        )
    return result


class ReviewReadArguments(Arguments):
    resource: Literal["candidates"]
    trip_plan_id: str
    query_id: str
    result_revision: int = Field(ge=1)
    offset: int = Field(default=0, ge=0)


class ReplanArguments(Arguments):
    trip_plan_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)


def trip_review_registry(registry, trip_plan_id, candidate_queries=None, retry_queries=()):
    if "write_trip" not in registry:
        return {}
    tool = registry["write_trip"]
    allowed_trips = set(trip_plan_id) if isinstance(trip_plan_id, list) else {trip_plan_id}
    expand = tool.expand_queries or getattr(tool.handler, "expand_queries", None)

    def bound_queries(args, turn):
        if args["tripplan"].get("trip_plan_id") not in allowed_trips:
            raise ValueError("review_trip_mismatch")
        return expand(args, turn) if expand else []

    result = {
        "write_trip": replace(
            tool,
            description="局部修复或选择当前TripPlan。amend同次提交全部关联变化和受影响查询items，"
            "程序批量执行，无需另调query；观察结果再select。content只存目的地活动，"
            "自行补充的退房等辅助软活动可提交content=[]删除。reply引用实际保存内容。",
            arguments=ReviewTripArguments,
            expand_queries=bound_queries,
        )
    }

    def replan_scope(args, turn):
        from trip_assistant.runtime.errors import ToolFailure

        if not set(args["trip_plan_ids"]) <= allowed_trips:
            raise ToolFailure("REPLAN_TARGET_MISMATCH", "replan is limited to current TripPlans")
        return []

    async def replan(args, turn):
        return {"status": "ok", "trip_plan_ids": args["trip_plan_ids"], "reason": args["reason"]}

    result["request_replan"] = ToolDefinition(
        "request_replan",
        "仅需要整体策略变化或新的独立查询时交回主规划。填写当前行程ID及原因，单独提交；"
        "不修改方案、不放宽硬条件、不重置预算。局部软修复继续write_trip.amend。",
        "control",
        ReplanArguments,
        replan,
        replan_scope,
    )
    if retry_queries and "query" in registry:
        query_tool = registry["query"]
        query_expand = query_tool.expand_queries or getattr(
            query_tool.handler, "expand_queries", None
        )

        def bound_query(args, turn):
            from trip_assistant.memory.planning import query_intent

            if any(query_intent(item) not in retry_queries for item in args["query_plan"]):
                raise ValueError("review_query_scope")
            return query_expand(args, turn) if query_expand else []

        result["query"] = replace(
            query_tool,
            expand_queries=bound_query,
            description="只重试本轮已失败的原独立query_plan，条件不变，可设置refresh=true；行程待查项由状态机执行。",
        )
    return result


class PolicyArguments(Arguments):
    context: dict = Field(default_factory=dict)
    trip_plan_id: str | None = None


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    category: str
    arguments: type[Input]
    handler: object = None
    expand_queries: object = None
    followup: object = None
    capabilities: str = ""

    @property
    def schema(self):
        schema = self.arguments.model_json_schema()
        if issubclass(self.arguments, TripArguments):
            schema.get("properties", {}).pop("goal_assessment", None)
            if not issubclass(self.arguments, SinglePassTripArguments):
                schema.get("properties", {}).pop("final_answer", None)
            schema.get("$defs", {}).pop("GoalAssessment", None)
        definitions = schema.get("$defs", {})
        for definition in list(definitions.values()):
            for key in ("conditions", "confirmed_conditions"):
                fields = definition.get("properties", {})
                if key in fields:
                    if fields[key].get("$ref") == "#/$defs/SharedPlanConditions":
                        continue
                    fields[key] = {"$ref": "#/$defs/PlanningConditions"}
                    definitions["PlanningConditions"] = CONDITIONS
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description
                + self.capabilities
                + (
                    TRIP_ARGUMENT_HELP
                    if issubclass(self.arguments, TripArguments)
                    and not issubclass(self.arguments, SinglePassTripArguments)
                    else ""
                ),
                "parameters": compact_schema(
                    schema, omit_descriptions=issubclass(self.arguments, TripArguments)
                ),
            },
        }

    def validate(self, value):
        model = self.arguments.model_validate(value)
        result = model.model_dump(exclude_none=True)
        if isinstance(model, TripArguments):
            result["tripplan"] = model.tripplan.model_dump(exclude_unset=True)
            result["tripplan"]["operation"] = model.tripplan.operation
        return result


def build_registry(handlers):
    specs = [
        (
            "read",
            "读取行程/会话摘要及按ID精读历史；摘要每页5条，不提供候选翻页。"
            "参数已知时与其他独立工具同批提交；未知ID须先观察摘要再精读。",
            "read",
            ReadArguments,
        ),
        (
            "query",
            "独立外部查询入口，只接收query_plan。TripPlan待查item由状态机自动执行，不能在这里重复填写。"
            "同批包含行程规划时本工具request_kind也用planning；纯查询才lookup。"
            "独立结果与规划结果一起回总结；已覆盖的查询通过有效缓存复用。",
            "query",
            QueryArguments,
        ),
        (
            "write_memory",
            "增加、替换、撤回长期偏好；明确偏好更新加规划时与write_trip同次提交，不需先写再读。"
            "用户明确要求重新分析时operation=reanalyze委派偏好Agent，不修改TripPlan。",
            "simple_write",
            MemoryArguments,
        ),
        (
            "write_trip",
            "创建、选择或原子修改行程。",
            "complex_write",
            TripArguments,
        ),
        (
            "query_policy",
            "仅用户明确出差或明确要求政策时使用；普通旅游/默认旅游/普通查询禁止调用。"
            "取得适用政策小结、强制约束及证据，与参数已知且不依赖政策的必要读查同批提交。"
            "收到整批结果后再生成TripPlan，不与行程规划/选择/修改/确认同批。",
            "policy",
            PolicyArguments,
        ),
    ]

    def policy_queries(args, turn):
        return ["policy:" + json.dumps(args, sort_keys=True, ensure_ascii=False)]

    return {
        name: ToolDefinition(
            name,
            description,
            category,
            args,
            handlers.get(name),
            policy_queries if category == "policy" else None,
        )
        for name, description, category, args in specs
    }
