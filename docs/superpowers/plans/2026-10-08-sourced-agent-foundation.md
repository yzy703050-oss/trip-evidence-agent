# V0 Sourced Agent Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 V0 增加火车、酒店和攻略子 Agent 的可信数据契约及调度路径，使无来源价格不再进入行程和预算输出。

**Architecture:** 保留 AgentScope `Msg`、Skill 懒加载和现有 CLI。新增领域模型与 Provider 协议；三个 Agent 只调用注入的 Provider 并输出统一状态与来源。调度器按依赖整理模型提出的计划，行程规划只消费查询结果，费用由确定性代码计算。

**Tech Stack:** Python 3.11、AgentScope 1.0.16、Pydantic 2、pytest/pytest-asyncio、Rich CLI。

**Spec:** [`docs/superpowers/specs/2026-10-08-agent-data-expansion-design.md`](../specs/2026-10-08-agent-data-expansion-design.md)，本计划只实施其中“数据契约与调度边界”阶段。真实平台适配器和攻略工具链分别在账号权限核对后另写实施计划，不在本计划中虚构 API 参数。

## Global Constraints

- 只修改独立 V0 仓库；不引入 V1/V2 应用层。
- 无平台接口或凭据时，生产路径返回 `unavailable`，不生成演示报价。
- 价格、库存、开放与预约状态必须带来源和带时区查询时间；缺项保留 `null` 或明确状态。
- `estimated_budget` 旧字段可继续读取，但不再作为“已核实费用”展示。
- Provider 假实现仅供自动测试；CI 不调用真实平台或大模型。
- 各任务完成后运行自己的测试并提交；最终运行完整离线回归。

## Review Focus

- 模型把火车与事项收集排在同批时，程序仍先收集查询条件：任务 3 测试。
- 平台返回价格但未说明税费或查询时间时，不可得出完整总预算：任务 1、4 测试。
- 平台超时与空库存是不同状态，不能都显示成“无票/无房”：任务 2、4 测试。
- 用户画像推断的地点与缺失日期不能悄悄变成已确认的查询条件：任务 2、3 测试。
- 规划模型自行写出的报价不能穿过最终展示：任务 4 测试。

---

### Task 1: 领域契约与确定性费用

**Files:**
- Create: `travel_data/contracts.py`, `travel_data/budget.py`, `travel_data/__init__.py`
- Test: `tests/test_travel_data_contracts.py`, `tests/test_travel_budget.py`

**Interfaces:**
- `Source(provider: str, fetched_at: datetime, url: str | None)`：`fetched_at` 必须带时区。
- `AgentDataResult(status: Literal["ok", "partial", "needs_input", "unavailable", "error"], query: dict, items: list[dict], missing_fields: list[str], source: Source | None, fetched_at: datetime | None, message: str | None)`：`to_dict()` 输出可 JSON 序列化的字典。
- `TrainOffer`：`id`、车次、出发/到达站与时间、席别、`price_cny: Decimal | None`、`availability`、`remaining: int | None`、`source`、`url`。
- `HotelOffer`：`id`、酒店/房型、入住/离店日期、人数、`stay_total_cny: Decimal | None`、`fees_included: bool | None`、`availability`、`cancellation`、`source`、`url`。
- `GuideFact`：地点或事实类型、内容、`verification: Literal["verified", "needs_check"]`、`source: Source | None`。
- `TrainQuery(origin: str, destination: str, departure_date: date, passengers: int = 1)`、`HotelQuery(city: str, check_in: date, check_out: date, guests: int)`、`GuideQuery(destination: str, visit_dates: list[date])`：出行条件模型；酒店离店日期必须晚于入住日期。
- `BudgetBreakdown(known_subtotal_cny: Decimal, lines: list[dict], missing_categories: list[str], complete: bool)` 与 `build_budget(train: TrainOffer | None, hotel: HotelOffer | None, passengers: int, required: set[str]) -> BudgetBreakdown`：仅使用有效报价。

- [ ] **Step 1: Write failing tests.** 断言无时区的 `Source`、负价、缺少来源的“已核实”报价和无效酒店日期被拒绝；序列化保留 `null`；一张 500 元车票乘 2 人加含税费 800 元酒店得到 1800 元；酒店税费未知时 `complete=False`、酒店列入缺项。
- [ ] **Step 2: Run failing tests.** `python -m pytest -q tests/test_travel_data_contracts.py tests/test_travel_budget.py`；预期因新模块不存在而失败。
- [ ] **Step 3: Implement the listed models and `build_budget`.** 金额使用 `Decimal`，对外 JSON 金额用字符串；不接受模型文本作为报价输入。
- [ ] **Step 4: Verify.** 同一步骤 2 的命令全通过，再运行 `python -m pytest -q tests --ignore=tests/test_intention_agent.py`。
- [ ] **Step 5: Commit.** `git add travel_data tests/test_travel_data_contracts.py tests/test_travel_budget.py`；提交 `feat: add sourced travel data contracts`。

### Task 2: 三个领域 Agent 与 Provider 边界

**Files:**
- Create: `travel_data/providers.py`
- Create: `.claude/skills/train-search/SKILL.md`, `.claude/skills/train-search/script/agent.py`
- Create: `.claude/skills/hotel-search/SKILL.md`, `.claude/skills/hotel-search/script/agent.py`
- Create: `.claude/skills/travel-guide/SKILL.md`, `.claude/skills/travel-guide/script/agent.py`
- Modify: `agents/lazy_agent_registry.py`, `.claude/skills/README.md`
- Test: `tests/test_sourced_agents.py`, `tests/test_lazy_agent_registry.py`

**Interfaces:**
- `TrainProvider.search(query: TrainQuery) -> AgentDataResult`、`HotelProvider.search(query: HotelQuery) -> AgentDataResult`、`GuideProvider.search(query: GuideQuery) -> AgentDataResult`，均为 `async`；Query 模型定义在 `travel_data/contracts.py`。火车要求起终点和日期；酒店要求城市、入住/离店日期及人数；攻略至少要求目的地。
- 三个 Agent 类分别为 `TrainSearchAgent`、`HotelSearchAgent`、`TravelGuideAgent`；构造函数接受 `name`、`model=None`、`provider=None`，`reply(Msg) -> Msg` 返回 `AgentDataResult.to_dict()` 的 JSON。
- `LazyAgentRegistry(..., providers: Mapping[str, object] | None = None)` 对调度名 `train_search`、`hotel_search`、`travel_guide` 注入相应 Provider。未注入时使用返回 `unavailable` 的默认实现。

- [ ] **Step 1: Write failing tests.** 用内存 Provider 验证三个 Agent 的查询条件、结构化响应与来源原样传递；缺少必填条件返回 `needs_input` 且不调用 Provider；Provider 超时返回 `error`，空库存保持 `ok` 且 `items=[]`；未注入时返回 `unavailable`；懒加载可找到三个调度名。
- [ ] **Step 2: Run failing tests.** `python -m pytest -q tests/test_sourced_agents.py tests/test_lazy_agent_registry.py`；预期因类/映射不存在而失败。
- [ ] **Step 3: Implement Provider 协议、默认不可用实现及 Agent。** Agent 不调用 LLM 补报价；从 `Msg.content` 的 `context` 与 `previous_results` 读取经事项收集确认的字段。扩展注册器时保留现有六个 Agent 的加载行为。
- [ ] **Step 4: Verify.** 同一步骤 2 的命令全通过，并运行 `python -m pytest -q tests --ignore=tests/test_intention_agent.py`。
- [ ] **Step 5: Commit.** 提交新增 Skill、Provider 与注册器改动，消息 `feat: add train hotel and guide agent boundaries`。

### Task 3: 意图与确定性依赖调度

**Files:**
- Modify: `agents/intention_agent.py`, `agents/orchestration_agent.py`, `.claude/skills/event-collection/script/agent.py`, `.claude/skills/event-collection/SKILL.md`, `utils/skill_loader.py`（仅在映射需要时）
- Test: `tests/test_sourced_routing.py`

**Interfaces:**
- `normalize_schedule(schedule: list[dict]) -> list[dict]` 放在 `agents/orchestration_agent.py`：保留有效的既有 Agent；`train_search`、`hotel_search`、`travel_guide` 在 `event_collection` 后执行；`itinerary_planning` 在本轮要求的领域查询后执行；相同领域只执行一次。
- 意图 Agent 的提示词和 Skill 映射新增三个调度名。只问票或酒店时无需额外安排完整行程；只问普通天气仍可使用 `information_query`。

- [ ] **Step 1: Write failing tests.** 输入打乱的模型计划，断言事项收集先于领域查询、规划最后执行；火车和酒店同批并行；重复项去重；孤立火车查询会补上事项收集；普通天气不强制新增攻略 Agent；原有政策与记忆查询顺序不变；人数与酒店入住/离店日期由事项收集输出，缺失时保持缺失状态。
- [ ] **Step 2: Run failing tests.** `python -m pytest -q tests/test_sourced_routing.py`；预期新调度规则缺失而失败。
- [ ] **Step 3: Implement `normalize_schedule` 并在执行前调用。** 用代码依赖关系修正优先级，不从模型的 `reasoning` 推断价格或库存。保留每个 Agent 的成功/失败隔离及运行记录。
- [ ] **Step 4: Verify.** 同一步骤 2 的命令全通过，并运行 `python -m pytest -q tests --ignore=tests/test_intention_agent.py`。
- [ ] **Step 5: Commit.** 提交 `feat: enforce sourced agent dependencies`。

### Task 4: 规划、CLI 与评测的来源保护

**Files:**
- Modify: `.claude/skills/plan-trip/SKILL.md`, `.claude/skills/plan-trip/script/agent.py`, `cli.py`, `evals/v0_memory/runner.py`, `README.md`
- Create: `travel_data/plan_guard.py`
- Test: `tests/test_sourced_itinerary.py`, `tests/test_sourced_cli.py`, `tests/test_sourced_evals.py`

**Interfaces:**
- `guard_itinerary(plan: dict, results: list[dict]) -> dict`：只允许计划引用 `results` 中存在的报价 `id`；对用户可见计划采用字段白名单，只保留日期、时间、活动地点等建议字段，价格、车次、房型、天气、开放与预约事实只能从对应查询结果重建；旧 `estimated_budget` 不计入结构化费用。
- 规划结果新增 `selected_train_id`、`selected_hotel_id` 和 `budget`，其中 `budget` 由 Task 1 的 `build_budget` 生成；无完整报价时只显示小计和缺项。
- CLI 为三个新 Agent 显示状态、候选项、来源与查询时间；`unavailable`/`needs_input`/`error` 不能显示为“无票”或“无房”。

- [ ] **Step 1: Write failing tests.** 模型在 `description`、`notes` 或 `estimated_budget` 中输出虚构的 299 元车票或“今日开放”时最终展示不包含这些断言；引用已查询的报价 ID 时展示其真实价格与来源；部分失败仍展示其余结果；旧 `estimated_budget` 不触发预算内保证；EDD 对无来源实时报价判失败。
- [ ] **Step 2: Run failing tests.** `python -m pytest -q tests/test_sourced_itinerary.py tests/test_sourced_cli.py tests/test_sourced_evals.py`；预期因保护函数或显示分支不存在而失败。
- [ ] **Step 3: Implement guard、预算注入、CLI 展示和 EDD 检查。** 将规划提示词限制为“引用候选项 ID 与生成建议”；对模型输出再由程序校验，不能只依赖提示词。更新 README 说明尚待接入真实平台。
- [ ] **Step 4: Verify.** 同一步骤 2 的命令全通过；运行 `python -m pytest -q tests --ignore=tests/test_intention_agent.py`、`python -m compileall -q agents travel_data .claude/skills cli.py` 和 `git diff --check`；手工对照设计文档的五个验收场景检查测试证据。
- [ ] **Step 5: Commit.** 提交 `feat: ground itinerary output in sourced results`。

## Completion

四项任务完成、完整离线回归通过，且新增测试证明“没有 Provider 时没有实时价格”和“有 Provider 结果时价格、来源与预算一致”，才算本计划完成。随后基于用户平台账号编写真实火车/酒店适配计划，再编写攻略工具接入计划。本计划不把假 Provider 的测试结果描述为真实查询能力。
