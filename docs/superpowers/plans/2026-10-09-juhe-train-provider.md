# 聚合数据火车 Provider 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将用户已开通的聚合数据“火车订票查询”站到站接口接入 V0，使配置 KEY 后可展示带来源和查询时间的车次席别报价与余票。

**Architecture:** 保留 `TrainSearchAgent` 和统一 `TrainProvider.search(TrainQuery) -> AgentDataResult` 契约。新适配器负责一次 POST 表单请求、响应校验和逐席别转换；CLI 只在本地 KEY 存在时注入该 Provider。酒店与攻略不受影响。

**Tech Stack:** Python 3.11、已安装的 requests 2.32.3、AgentScope、pytest。HTTP 调用在 `asyncio.to_thread` 中执行并带请求超时。

**Spec:** [`docs/superpowers/specs/2026-10-08-agent-data-expansion-design.md`](../specs/2026-10-08-agent-data-expansion-design.md) 的“聚合数据火车查询适配（2026-10-09）”；外部字段以[聚合数据官方接口文档](https://www.juhe.cn/docs/api/id/817)为准。

## Global Constraints

- 只接文档中的 `https://apis.juhe.cn/fapigw/train/query` 查询接口，不调用订票 MCP、不下单。
- KEY 只从本地 `.env` 的 `JUHE_TRAIN_API_KEY` 读取；不写入日志、异常、来源 URL、测试快照或 Git。
- `search_type=1`、`enable_booking=2`，原样传递已确认站名；北京时间今天起 15 个日历日以外不请求接口。
- 报价使用 `Decimal`，仅有可预订车次及明确正余票的席别为 `available`；未知数量、失败、空结果不得伪装成有票。
- 自动测试不得访问聚合数据。真实 KEY 联调须单独执行，且不属于离线测试通过条件。

## Review Focus

- 查询日期超出接口窗口时，返回范围不可用且零 HTTP 请求：Task 1。
- `num` 为“无”“有”、数字、未知值，及班次不可预订时，席别可订状态正确：Task 1。
- API 返回鉴权/额度错误、HTTP 错误或格式错误时，不能呈现为空车次或无票：Task 1。
- 一条车次含多个席别时，每席别有不同且稳定的 ID、价格和数量：Task 1。
- CLI 启动时有 KEY 注入真实适配器、无 KEY 保持 unavailable，任何输出都不泄露 KEY：Task 2。

---

### Task 1: 聚合数据响应到统一火车报价

**Files:**
- Create: `travel_data/juhe_train.py`
- Test: `tests/test_juhe_train_provider.py`

**Interfaces:**
- `JuheTrainProvider(api_key: str, *, http_post=None, now_fn=None, timeout: float = 10.0)` 实现 `async search(query: TrainQuery) -> AgentDataResult`。
- `http_post` 是可注入的同步请求函数，签名兼容 `requests.post(url, data=..., headers=..., timeout=...)`；测试使用本地假响应。`now_fn` 返回带时区的当前时间；未注入时使用北京时间。
- API 的 `result[].prices[]` 转换为 `TrainOffer.to_dict()`，候选项 ID 包含日期、车次、起终点和席别编码/名称；`Source.url` 为不带 KEY 的官方文档链接，单项 `url=None`。

- [ ] **Step 1: Write failing tests.** 验证 POST 表单只发一次且含 `search_type=1`、`enable_booking=2`；文档示例的二等座 627 元、余票 1 转成可订报价，商务座 `num=无` 为售罄；“有”保留 `remaining=null`，未知值不标可订；异常行导致 `partial`，整体格式错误为 `error`，合法空数组是 `ok`。覆盖鉴权/额度码、HTTP 超时、日期窗口、KEY 不出现在序列化结果和来源 URL。
- [ ] **Step 2: Confirm RED.** 运行 `python -m pytest -q tests/test_juhe_train_provider.py`，预期因 `JuheTrainProvider` 不存在而失败。
- [ ] **Step 3: Implement adapter.** 用 `requests.post` 与 `asyncio.to_thread`，不自动重试；将收到响应的时间记为带时区 `fetched_at`。只从合法字段建立 `TrainOffer`，跳过不完整行并返回明确状态。对非 0 `error_code` 分类为 `unavailable` 或 `error`，不透传可能含敏感数据的原始响应。
- [ ] **Step 4: Verify.** 同一聚焦命令全通过，再运行 `python -m pytest -q tests --ignore=tests/test_intention_agent.py`、`python -m compileall -q travel_data`。
- [ ] **Step 5: Commit.** `feat: adapt Juhe train query responses`。

### Task 2: 本地配置与 CLI 注入

**Files:**
- Modify: `config.py`, `.env.example`, `cli.py`, `README.md`
- Test: `tests/test_juhe_cli_wiring.py`

**Interfaces:**
- `Settings.juhe_train_api_key: str = ""` 读取 `JUHE_TRAIN_API_KEY`。
- CLI 构造 `LazyAgentRegistry(..., providers={"train_search": JuheTrainProvider(key)})`；KEY 为空时传空映射，维持现有默认 `UnavailableProvider`。

- [ ] **Step 1: Write failing tests.** 使用假 KEY 与猴子补丁替身初始化 CLI，断言注册器中的 `train_search` 收到 Juhe Provider、酒店仍未配置；KEY 为空时火车保持 `unavailable`。断言配置/显示路径不输出 KEY，并让一个文档形状响应经 Agent 与 CLI 展示来源、席别、价格、查询时间。
- [ ] **Step 2: Confirm RED.** 运行 `python -m pytest -q tests/test_juhe_cli_wiring.py`，预期因配置和注入缺失而失败。
- [ ] **Step 3: Wire and document.** 在 `.env.example` 增加空的 `JUHE_TRAIN_API_KEY`；在 README 说明从已开通接口的后台取得 KEY 并填入本地 `.env`、15 天和站名范围、查询可能计费、当前不支持下单。CLI 不在日志中打印 KEY。
- [ ] **Step 4: Verify.** 聚焦测试、完整离线测试、`python -m compileall -q agents travel_data .claude/skills cli.py`、`git diff --check` 均通过；手工核对设计中的所有聚合数据验收条件。
- [ ] **Step 5: Commit.** `feat: configure Juhe train provider in CLI`。

## Completion

两任务代码和测试通过、独立评审无重要缺陷，且已配置 KEY 的生产路径确实使用 Juhe Provider 时，离线接入完成。未提供本地 KEY 时不得声称已做真实联调；用户填入 KEY 后单独核对真实响应、计费和站名范围。此计划不修改酒店或攻略接口。
