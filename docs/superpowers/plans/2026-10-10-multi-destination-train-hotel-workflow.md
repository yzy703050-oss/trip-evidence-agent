# 多目的地火车与酒店任务循环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. 用户已选择当前分支单代理执行，逐项 TDD，完成后整体自审；不派任务子代理。

**Goal:** 实现可逐段规划、全程校验、询问用户后断点恢复的多目的地火车与酒店工作流。

**Architecture:** MainAgent 产生一次初始决策，行程决策携带完整任务提案；Harness 分配 ID、执行任务循环并保存状态。信息获取仍执行自己的工具循环，结果改为按查询和任务归属保存。确定性检查控制完成资格，主 Agent 负责草稿、冲突说明和最终总结。

**Tech Stack:** 现有 Python、AgentScope、pytest/pytest-asyncio、JSON 文件持久化、Juhe 火车及 AMap 酒店地点 Provider；不新增模型供应商、JSON 模式或 RAG 依赖。

**Spec:** [唯一正式设计](../specs/2026-10-09-multi-destination-train-hotel-workflow-design.md)，特别是第 5–12 节契约与 S01–S22 验收场景。本仓库没有初始化 OpenSpec，不创建第二套 proposal/specs/tasks；本计划只展开实施细节。

日期：2026-10-10。实施基线：`024faa6`，分支 `codex/information-acquisition-agent`。计划状态：已确认并实施，整体自审与人工规格核对完成。线上能力限制见[评估报告](../../evals/2026-10-10-multi-destination-workflow-evaluation.md)。执行方法沿用用户已有选择，不再询问单代理或多代理。

## Global Constraints

- 工作流状态：`running / needs_input / completed / partial / error`；任务状态：`pending / running / draft / needs_input / validated`。
- 本次多段工作流只安排火车和酒店，不生成 activities，不调用天气、攻略或网页工具；已有独立请求保持兼容。
- RAG 接口、实现、知识数据及外部 `MODULAR-RAG-MCP-SERVER` 不变。
- Harness 分配稳定任务/查询/消息 ID；默认值与建议不得写为用户确认条件或长期偏好。
- 人数默认 1；酒店人数沿用旅客人数；预算、席别不凭空设置硬限制；候选视图默认每查询 5 个。
- 高德返回 hotel_place，价格、库存和入住规则未知；不能证明报价请求或硬预算已经满足。
- 每任务最多 3 次信息获取；每次最多 6 次模型、10 次工具，单工具 30 秒；主决策上限 `4 × N + 4`，整轮工具上限 `10 × N`，另提供可配置整轮时限。
- 草稿齐全必须经过全程校验才完成；进入下一任务不清空本回合资源计数。
- 快照与候选完整持久化，原子写入加版本检查；不自动清理历史，不因写入失败整轮重放付费请求。

## Review Focus

- 新进程或另一会话提交旧版本：不能覆盖新快照，失败更新不得自动重跑外部查询。由任务 4 的并发与恢复测试覆盖。
- 相同车次在不同日期、同城不同人数：候选 ID、查询 ID 和版本必须同时匹配。由任务 2、3 的跨查询引用测试覆盖。
- 缺日期但可查酒店地点：完成可执行查询再集中追问必要日期，不循环追问普通默认项。由任务 1、5 测试覆盖。
- 刷新失败或供应商未配置：保留旧来源事实与取得时间，明确未核实，不能改写成无车或无房。由任务 2、5 测试覆盖。
- 模型要求 completed，但还有未知抵达日期、失效草稿或预算缺口：确定性拒绝，输出与保存使用同一份结果。由任务 3、5、6 测试覆盖。

## 文件与接口边界

| 文件 | 责任 |
| --- | --- |
| 新增 `agents/workflow_contracts.py` | 提案、动作、状态和条件来源契约；ID 和版本转换 |
| 新增 `agents/workflow_queries.py` | 工具结果到查询记录的适配、候选引用与结果视图 |
| 新增 `agents/workflow_guard.py` | 单段和全程确定性检查，预算与时间事实重建 |
| 新增 `agents/workflow_runner.py` | 调用 Main.step、分派信息获取、预算、断点与统一输出 |
| 新增 `context/workflow_store.py` | 按用户保存快照、版本比较、跨进程锁 |
| 修改 Main/Harness/RunState | 兼容旧入口，接入一次初始决策和新工作流 |
| 修改 Info/tools/candidates | 工作流作用域下按查询保存；兼容旧 domain_results |
| 修改 memory/session/telemetry/CLI | 活动索引、恢复、关联日志和一致展示 |
| 修改 query-info/plan-trip Skill | 当前任务查询、事实小结和交通住宿指南 |
| 新增测试与多目的地评估入口 | 覆盖 S01–S22，复用真实 CLI 初始化 |

Python 字典是协议载体，继续使用现有解析方法；新增验证不改变模型 API 配置。新模块的接口均返回可 JSON 序列化的数据。

## Task 1：任务提案、来源与版本契约

**Files:** Create `agents/workflow_contracts.py`; Modify `agents/contracts.py`; Test `tests/test_workflow_contracts.py`。

**Interfaces:**
- `create_workflow(proposal: dict, context: dict, *, workflow_id: str | None = None) -> dict`：校验顺序任务、路线、依赖和酒店作用域，Harness 分配 ID，创建第 6 节快照。
- `effective_conditions(confirmed: dict, task_conditions: dict, preferences: dict) -> tuple[dict, dict]`：返回有效条件及 field_sources，不修改输入。
- `validate_action(action: dict, workflow: dict) -> dict`：验证第 8.5 节五类动作、当前版本、允许字段；不执行副作用。
- `apply_user_update(workflow: dict, updates: dict) -> dict`：只接受关联本轮用户答复的字段；保留任务 ID，递增受影响版本，标记下游待检查并保留失效草稿供审计。
- `RunState` 新增可选工作流/当前任务引用和查询记录容器；旧字段、构造方式及单项查询行为保持兼容。

- [x] **RED：写条件默认、非法提案、修订测试。** 用 fixture 定义上海→北京→杭州→上海三段提案，明确 start_date 和每段停留条件；断言如下，并加入负人数、依赖环、路线断裂、越权状态覆盖和版本过期动作测试。

```python
def test_defaults_do_not_become_confirmed(route_proposal):
    workflow = create_workflow(route_proposal, {"original_query": "安排火车酒店"})
    assert workflow["effective_conditions"]["passengers"] == 1
    assert workflow["field_sources"]["passengers"] == "default"
    assert workflow["effective_conditions"]["guests"] == 1
    assert workflow["confirmed_conditions"].get("passengers") is None
    assert workflow["effective_conditions"]["constraints"] == {}
    assert [t["status"] for t in workflow["tasks"]] == ["pending"] * 3
    assert workflow["tasks"][-1]["requires_hotel"] is False
```

- [x] **RED 验证：** `& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_workflow_contracts.py -q`，预期新增接口尚不存在导致失败；随后负例必须是契约拒绝而非 fixture 错误。
- [x] **实现：** 按第 5、6、11 节实现上述纯函数；条件合并只使用输入中有依据的日期，原文相对日期解释放在 Main 的提案里并绑定 context.current_time。默认、proposal 和 derived 单独标来源。
- [x] **GREEN：** 同一命令通过，并断言修改 task_002 保留 task_001；人数 3 使费用相关版本失效，地点资料保留。
- [x] **记录并提交：** 本任务勾选、记录测试证据；`git add agents/workflow_contracts.py agents/contracts.py tests/test_workflow_contracts.py`，`git commit -m "feat: define versioned travel workflow contracts"`。

验收映射：S01、S03、S07、S08、S14、S18–S22。

## Task 2：独立查询结果、候选池与可信时间

**Files:** Create `agents/workflow_queries.py`; Modify `travel_data/tools.py`, `travel_data/candidates.py`, `travel_data/contracts.py`, `travel_data/juhe_train.py`, `.claude/skills/query-info/script/agent.py`; Test `tests/test_workflow_queries.py`, `tests/test_juhe_train_provider.py`, `tests/test_information_agent_loop.py`。

**Interfaces:**
- `record_query(workflow: dict, task_id: str, parameters: dict, constraints: dict, result: dict, execution: dict, *, domain: str, refresh: bool = False) -> dict`：按任务归属与规范化参数定位查询；新参数新 ID，成功刷新增版本，失败保留旧事实并记录刷新失败。
- `query_views(workflow: dict, task_id: str, *, limit: int = 5, offset: int | None = None) -> list[dict]`：读取持久化完整池，输出窗口；None 沿用当前查询的翻页位置，不发起外部请求。
- `resolve_selection(workflow: dict, task_id: str, task_revision: int, selection: dict) -> dict`：严格按查询/版本/候选 ID 重建事实，失配抛 ValueError。
- `CandidateStore.snapshot() -> dict`、`CandidateStore.restore(snapshot: dict) -> None`：保存/恢复完整 AgentDataResult、来源与取得时间。
- `TrainOffer` 追加可选 `departure_at / arrival_at / time_evidence`，默认未知，保留已有位置参数兼容。出发日期绑定查询；抵达只从明确日偏移或已核实耗时构造。
- Info `run(context, run)` 在 task_request 模式返回第 8.3 节 task_result；旧模式继续原返回。工具按当前任务写查询记录，不回填全程 confirmed_conditions。

- [x] **RED：** 测试去程/返程相同领域分别保存、不同日期候选拒绝混用、同参数缓存复用、刷新失败保留来源、offset 本地读取、窗口 5 不截断持久化池、越域工具拒绝、模型伪造事实不进入 query_results。测试用固定时间及离线 Provider，不访问真实服务。

```python
def test_later_query_preserves_first_leg(populated_workflow):
    w = populated_workflow
    rows = list(w["results_by_query"].values())
    assert len({r["id"] for r in rows if r["domain"] == "train"}) == 3
    assert w["confirmed_conditions"]["start_date"] == "2026-10-11"
    assert rows[0]["parameters"]["origin"] == "上海"
    assert len(query_views(w, w["tasks"][0]["id"])[0]["items"]) == 5
    assert len(rows[0]["items"]) == 8
```

- [x] **RED 验证：** `& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_workflow_queries.py tests/test_juhe_train_provider.py -q`，新增查询隔离/时间断言失败。
- [x] **实现：** 工具事实由程序登记，Info 只能给小结和已有引用；在工具调用入口限制 train/hotel。保留单项 domain_results 适配。统计来自实际 model/tool/cache 路径，不能采用模型回填值。
- [x] **时间测试与实现：** 固定 departure_date+可靠时刻能生成 departure_at；当前 Juhe 样例没有可靠抵达日期时 arrival_at 必须 null。新增标准 TrainOffer 测试用带来源的跨日 arrival_at；仅在 Provider 字段确有已验证定义时解析其日偏移/耗时，不猜字段和次日。
- [x] **GREEN：** 上述命令及 `tests/test_information_tools.py tests/test_information_agent_loop.py tests/test_information_query_agent.py` 全通过。
- [x] **记录并提交：** 暂存本任务列出的文件，`git commit -m "feat: preserve task-scoped query evidence and candidates"`。

验收映射：S04、S05、S08、S10–S12、S14、S16、S22。

## Task 3：草稿及全程确定性校验

**Files:** Create `agents/workflow_guard.py`; Modify `travel_data/budget.py`（只在复用计算确需时）; Test `tests/test_workflow_guard.py`, `tests/test_travel_budget.py`。

**Interfaces:**
- `check_task(workflow: dict, task_id: str, draft: dict) -> dict`：通过 task_revision 与 resolve_selection 检查引用、时间/住宿/人数，返回 `valid / issues / reconstructed_plan`。
- `check_workflow(workflow: dict) -> dict`：返回 `valid / issues / reconstructed_tasks / budget / validated_revisions`；不修改任务状态。
- `finalize_workflow(workflow: dict, check: dict) -> dict`：验证检查对应所有当前版本后原子更新 validated/completed；不接受模型自行宣告完成。
- issue 字典使用 `code / task_ids / blocking / message / evidence`，供 Main 解释及生成建议，错误事实不由模型改写。

- [x] **RED：** 使用任务 2 的带来源候选 fixture，覆盖可靠跨日、未知抵达阻断必要衔接、固定日期冲突、住宿离店先于入住、返程覆盖、陈旧查询、库存不足、总预算 Decimal 汇总及 hotel_place 未知费用。

```python
def test_hotel_place_cannot_verify_hard_budget(budget_workflow):
    checked = check_workflow(budget_workflow)
    assert checked["valid"] is False
    assert checked["budget"]["verified"] is False
    assert "hotel_price_unknown" in {i["code"] for i in checked["issues"]}
    assert all(t["status"] == "draft" for t in budget_workflow["tasks"])
```

- [x] **RED 验证：** `& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_workflow_guard.py -q`，预期缺新 guard；补齐接口后逐项验证真实约束失败。
- [x] **实现：** 来源事实重建优先于模型 schedule；明确未知不计为 0。未知值只有阻止请求目标或衔接才为 blocking；地点推荐不要求伪造报价。用当前全部任务版本签名绑定全程检查。
- [x] **GREEN：** 同一命令及 `tests/test_travel_budget.py tests/test_amap_hotel.py` 通过；修订后旧检查不能完成新版本。
- [x] **记录并提交：** 暂存本任务文件，`git commit -m "feat: validate train hotel drafts across the whole route"`。

验收映射：S03、S05、S06、S12–S14。

## Task 4：快照、活动索引及断点恢复

**Files:** Create `context/workflow_store.py`; Modify `context/memory_manager.py`, `context/session_store.py`; Test `tests/test_workflow_store.py`, `tests/test_v0_compaction.py`。

**Interfaces:**
- `WorkflowStore(root: str | Path, user_id: str)`；`load(workflow_id: str) -> dict | None`、`list_active() -> list[dict]`、`save(workflow: dict, *, expected_revision: int | None) -> dict`。
- 新快照 expected_revision=None；更新必须匹配已保存版本，保存成功后递增工作流 revision；不递增未变化的 task_revision。`WorkflowConflictError` 区分过期提交。
- `MemoryManager.workflow_store`；`get_active_workflows() -> list[dict]`、`set_active_workflow(workflow_id: str | None) -> None`；session state 原字段保留，只增加活动索引。

- [x] **RED：** 创建临时存储，测试重新构造 MemoryManager 后读取完整候选/断点、用户隔离、危险路径拒绝、两个实例的过期 CAS、两个进程互斥、写入失败保留原文件、文本压缩后索引与快照不变。

```python
def test_stale_writer_cannot_overwrite(tmp_path, workflow):
    a = WorkflowStore(tmp_path, "alice")
    saved = a.save(workflow, expected_revision=None)
    stale = a.load(saved["id"])
    newer = a.save(saved, expected_revision=saved["revision"])
    with pytest.raises(WorkflowConflictError):
        WorkflowStore(tmp_path, "alice").save(stale, expected_revision=stale["revision"])
    assert a.load(saved["id"])["revision"] == newer["revision"]
```

- [x] **RED 验证：** `& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_workflow_store.py -q`。
- [x] **实现：** 复用 storage_component 与 atomic_json_write；采用工作流粒度 OS 文件锁，Windows/Linux 使用标准库平台适配，锁内 load/CAS/replace。完整持久化 results_by_query 与候选；不以日志回放重建收费请求。
- [x] **GREEN：** 同一命令及 `tests/test_v0_session_store.py tests/test_v0_cross_session.py tests/test_v0_compaction.py` 通过。
- [x] **记录并提交：** 暂存本任务文件，`git commit -m "feat: persist resumable travel workflows with revision checks"`。

验收映射：S07–S09、S14、S22。

## Task 5：主 Agent 循环、前置阶段和统一输出

**Files:** Create `agents/workflow_runner.py`; Modify `agents/main_agent.py`, `agents/execution_harness.py`, `agents/contracts.py`, `context/telemetry.py`, `cli.py`, `.claude/skills/query-info/SKILL.md`, `.claude/skills/plan-trip/SKILL.md`; Test `tests/test_workflow_runner.py`, `tests/test_workflow_main.py`, `tests/test_workflow_cli.py`, `tests/test_v0_telemetry.py`。

**Interfaces:**
- `MainAgent.initialize(context: dict) -> dict`：一次初始决策，保留旧模式并新增 workflow+synthesize 和 workflow_proposal。`plan` 作为兼容别名；Harness 不再额外调用 plan 后又 initialize。
- `MainAgent.step(context: dict) -> dict`：解析五类动作 JSON；Runner 在副作用之前执行 validate_action 并反馈有限修正。context 包含第 8.4 节各项，完整任务概览加当前候选窗口。
- `WorkflowRunner(main_agent, info_agent, memory_manager, *, limits, emit=None)`；`run(context: dict, run: RunState, workflow: dict) -> dict`：主循环、回合预算和持久化。
- 恢复仍经 initialize(context)：输入活动快照、checkpoint、原文和本轮答复，输出恢复目标与用户字段更新提案；由 apply_user_update 校验后进入 step。不能靠自由文本直接替换已确认字段；多规划无明确目标返回选择问题。
- `model_scope(**ids)` ContextVar 上下文管理器：turn_id/workflow_id/task_id/task_revision/message_id；MeteredModel 保留现有 stage/usage 字段并追加关联信息。
- 最终 envelope：原有 status/final_answer/agent_results 保留，追加 workflow_id/workflow_revision/workflow/stop_reason/gaps；工作流结果不走旧 daily_plans guard。

- [x] **RED：** 写可控 Main+真实 Info.run+离线 Provider 的三段/五段链路测试，观察 action、保存事件、实际调用计数。测试首段 draft 后继续第二段、全程 check 后才 completed、偏好先于查询且只执行一次。

```python
@pytest.mark.asyncio
async def test_all_drafts_still_require_global_check(workflow_runtime):
    result = await workflow_runtime.run_three_tasks()
    assert workflow_runtime.saved_status_after_first_task == "draft"
    assert "validate_workflow" in workflow_runtime.actions
    assert result["status"] == "completed"
    assert all(t["status"] == "validated" for t in result["workflow"]["tasks"])
    assert workflow_runtime.queried_domains == {"train", "hotel"}
```

- [x] **RED 验证：** `& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_workflow_runner.py tests/test_workflow_main.py tests/test_workflow_cli.py -q`。
- [x] **接入初始分支：** 扩展 validate_plan；前置偏好/记忆/制度继续旧调度且仅一次；workflow 模式不把信息获取合并成一次全局调用，直接交 Runner。单项、旧 itinerary 响应与已有 fake Main.plan 测试兼容。
- [x] **实现动作循环：** dispatch 保存可信 task_result；draft_task 检查引用与单段衔接，保存 draft；ask_user 原子保存 checkpoint 后返回；validate_workflow 回传 guard；finish 仅在当前校验通过才 completed，否则反馈或有依据 partial。拒绝旧版本异步结果覆盖。
- [x] **实现恢复：** 用户修改 task_001 从该段开始，下游重检；修改 task_002 保留 task_001 查询。新回合资源重置、跨回合审计保留；缓存引用可读并标 fetched_at，价格库存需要重确认时明确标记。
- [x] **实现防重复：** 记录动作/参数/任务版本/观察签名；没有新观察的重复动作停为 partial。未配置服务/酒店报价能力不足不盲重试；有明确瞬时失败依据最多额外重试一次，计入原预算。调用前预留预算，超时保存已有结果，不重跑整轮。
- [x] **实现 Msg/统计/展示：** Harness 附加公共关联字段与 in_reply_to；Info 汇总真实计数；模型 scope 使用 ContextVar 防止并行混串。CLI 直接展示/保存同一已校验工作流结果，不第二次删日期。
- [x] **更新 Skill：** 在现有指南添加 workflow 作用域分支，不全局删除旧天气/攻略能力；新行程指南只写交通住宿。若需要改 Skill，先读取 writing-skills 技能再编辑。
- [x] **补足失败测试并 GREEN：** 固定日期冲突→checkpoint→用户确认恢复、返程空结果 vs unavailable、缺日期仍查地点、模型非法 action/候选不触发 API、达到预算/无进展保留草稿、保存失败不重放收费查询、RAG 原协议兼容。上述命令及原 main/info/CLI 回归通过。
- [x] **记录并提交：** 暂存本任务文件，`git commit -m "feat: run main-agent travel task loops with checkpoints"`。

验收映射：S01–S03、S06–S11、S13–S22。

## Task 6：全量回归、真实链路评估与交付审核

**Files:** Create `evals/main_agent/multi_destination_runner.py`, `evals/main_agent/multi_destination_cases.json`, `tests/test_workflow_evals.py`, `docs/evals/2026-10-10-multi-destination-workflow-evaluation.md`; Modify 本计划的实施记录；既有设计只更新执行状态和证据链接，不另定行为。

**Interfaces:** 新 runner 提供 `python -m evals.main_agent.multi_destination_runner --offline` 及 `--live --output <path>`，复用 CLI 初始化、实际 Harness/Info/Provider，不创建另一套评估编排。offline 使用可控模型/供应商；live 输出逐案例状态、失败阶段、调用数量、workflow/task/query ID、stop_reason、引用核查及脱敏日志路径。

- [x] **RED：** 写评估断言测试，确保 completed 缺全程验证/任务版本失配/存在虚构 hotel_price 时评估失败；needs_input 因缺关键日期为预期暂停，不能冒充外部查询成功。
- [x] **RED 验证：** `& '..\..\.venv\Scripts\python.exe' -m pytest tests/test_workflow_evals.py -q`。
- [x] **实现并 GREEN：** 构造 S01–S22 案例和证据检查器，同一命令通过，`--offline` 全部符合指定场景预期。
- [x] **全量检查：** `& '..\..\.venv\Scripts\python.exe' -m pytest -q`，要求无新增失败；读取 skip 原因。`git diff --check` 无错误。核对冻结 RAG 和非本次文件没有改动。
- [x] **真实评估：** 使用已有配置运行 --live，日期按当前北京时间生成且落在 Juhe 查询窗口。至少覆盖三段、五段、修改第二段/恢复、hotel_place 能力不足及单项查询回归。实际供应商不支持可靠抵达日期时记录 partial 或 needs_input 的真实原因，不修改事实为完成，不用模拟报价冒充线上结果。
- [x] **整体自审及规格审核：** 对 S01–S22 逐条列测试/运行证据；检查前置角色一次执行、状态转换、候选事实、预算、时间、断点保存失败、版本并发、输出一致性和 API 脱敏。发现缺陷先 RED 修复后复测。
- [x] **文档与提交：** 写明通过数、跳过/限制、live 实际结果及调用链；`git commit -m "test: verify multi-destination workflow and recovery"`。无 OpenSpec verify/archive 可运行，报告人工核对结果，不声称使用了这些工作流。

完成条件：任务 1–6 有代码与实际验证证据，S01–S22 均有可追溯覆盖；无重要规格偏差和失败测试。线上能力限制如实报告，并区分实现通过与具体行程不能完成。

## 实施记录

| 阶段 | 状态 | 证据 |
| --- | --- | --- |
| 基线全量测试 | 已运行 | 2026-10-10：277 passed, 3 skipped, 1 warning；DashScope 已弃用接口提示为现有警告 |
| 技术复核 | 已完成 | 当前代码仍为 plan→分派→finalize；domain_results 单领域保存；无独立 workflow store；本计划按这些接口接入 |
| 计划自审 | 已完成 | 第 1–13 节设计职责及 S01–S22 均映射到上述任务；沿用单代理，不新增 JSON 模式或 RAG 改造 |
| 用户计划审阅 | 已确认 | 2026-10-10：确认计划，开始实施；单代理 |
| Task 1 | 已完成 | `817b162`；全量 288 passed / 3 skipped |
| Task 2 | 已完成 | `6204379`；全量 295 passed / 3 skipped |
| Task 3 | 已完成 | `fd1fda8`；全量 304 passed / 3 skipped |
| Task 4 | 已完成 | `6ed9455`；全量 314 passed / 3 skipped；Windows 跨进程 CAS |
| Task 5 | 已完成 | `55fbc57`；全量 336 passed / 3 skipped |
| Task 6 | 已完成验证 | 最终 355 passed / 3 skipped；81 项验收测试与 S01–S22 全通过；六例线上契约/边界检查通过，完整多段 completed 未覆盖 |
| 整体自审与人工规格核对 | 已完成 | 见评估报告：日期、晚数、城市名称、候选窗口、过期证据、并发恢复及模型动作修正均有测试证据 |
| 分支收尾 | 保留当前分支 | 按用户选择在当前分支本地提交，未请求合并、推送或 PR |
