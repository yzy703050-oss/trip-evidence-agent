# v0 长期居住城市与 CLI 自然语言回复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for single-agent execution, or superpowers:subagent-driven-development only when the user selects multiple agents. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 用户在首次使用本地 CLI 身份时必须填写长期居住城市并持久保存；CLI 的业务结果以自然语言段落显示。

**Architecture:** 复用 LongTermMemory 的 `home_location` 与 `source=user`，在 CLI 初始化阶段完成必填收集。根据用户后续澄清，在 Harness 最终返回边界统一生成完整的 final_answer，复用现有事实校验器；CLI 只展示该字段。其他结构化字段和 JSON 接口保持不变。

**Tech Stack:** Python 3.11、Rich、AgentScope、pytest。

**Spec:** 用户在本次聊天中确认“注册必须填写长期居住地”和“CLI 应返回一段话”；参考已有 `docs/design/v0-memory-redesign.md`、`docs/superpowers/specs/2026-10-10-planning-preflight-design.md`、`docs/superpowers/specs/2026-10-10-compact-intent-routing-design.md`。本 checkout 和父项目均未初始化 OpenSpec，没有对应 change 的 specs/design/tasks；不创建 OpenSpec，不声称运行 propose/apply/verify/archive。本文件只展开上述已确认要求的实现步骤。

## Global Constraints

- 修改持续开发的 `.superpowers/trip-evidence-agent` checkout，不改父项目或其他 v0 副本。
- v0 当前没有独立账号认证模块；“注册”落实为首次使用本地 user_id 时收集长期居住城市。缺少城市的旧用户补填一次，已填用户不重复询问。
- 保留现有 `home_location`，不增加平行的 home_city/residence 字段。输入去除首尾空白，空白不允许通过；不要求具体家庭地址，不引入城市数据库或模型解析调用。
- 用户显式填写的值持久保存，允许使用现有 `preferences set home_location 城市` 修改；临时出发地/目的地不写回居住城市。
- 长期居住城市是背景信息，不自动认定为本次出发城市。交通起点缺失仍沿用现有澄清流程，用户明确出发地优先。
- 内部 JSON、工作流、事实与报价校验、诊断日志接口保留；业务回复不直接显示字典、数组、字段键、None、True/False 或内部状态码。
- 自然语言呈现必须保留已校验的路线、日期、火车/酒店、价格及其单位、可用状态、来源链接与查询时间、地点参考与报价的区别、过期信息和未知费用。
- 有自然语言 final_answer 时保留其含义；结构化 final_answer 不原样输出，用已校验事实呈现。单纯 direct 回复及追问不附加无关详情。车次、报价、日期、来源和未核实事项必须在最终 JSON 的 final_answer 中，CLI 不另读 domain_results/workflow 来追加内容。
- 不增加一次额外 LLM 调用来改写回复，不改 MainAgent 的内部 JSON 协议，不引入依赖。

## Review Focus

1. 空格输入应继续追问；重启及跨会话恢复城市，同 user_id 隔离于其他用户。
2. 已有常住城市不重复询问，旧资料缺失时补填；Agent 不覆盖用户主动声明。
3. 结构化 final_answer、工作流 schedule 和旧 itinerary 都不泄漏 JSON；输入对象不得被呈现函数修改。
4. 正常报价、酒店地点参考、无价格和未知库存、重新核实要求必须区别表达，不能把服务失败说成无票/无房。
5. 错误、部分结果、补充条件与攻略/天气/网页结果均有可读回复；缺失字段采用中文说明，来源和时间保留。

## Task 1: 首次身份建立时保存必填长期居住城市

**Files:** Modify `cli.py`、`README.md`；Create `tests/test_cli_onboarding.py`；按初始化调用变化更新 `tests/test_juhe_cli_wiring.py`、`tests/test_amap_hotel_flow.py` 的启动依赖替身。

**Interfaces:** 新增 `TripEvidenceCLI.ensure_home_location() -> str`，使用 `self.memory_manager.long_term.get_preference('home_location')` 和 `set_user_preference('home_location', city)`。由真实 `initialize_system()` 在 MemoryManager 创建后、系统就绪前调用。后续上下文复用现有偏好注入。

- [x] 先写失败测试：空白后有效城市持续提示，重启读取城市、来源=user、多用户隔离、已有城市不询问、真实初始化触发收集、用户修改优先于 Agent、长期摘要区分居住地与本次起点。
- [x] 执行 `../../.venv/Scripts/python.exe -m pytest -q tests/test_cli_onboarding.py`，确认失败源于缺少收集逻辑而不是依赖替身错误。
- [x] 最小实现收集方法并接入初始化；长期上下文明确城市为背景，不据此确认本次起点；必要时在主 Agent 提示中补同一规则。
- [x] 修改 README 的启动说明和用户修改城市示例；仅修改因新增调用而不完整的初始化测试替身。
- [x] 执行 `../../.venv/Scripts/python.exe -m pytest -q tests/test_cli_onboarding.py tests/test_v0_memory_flow.py tests/test_v0_memory_context.py tests/test_juhe_cli_wiring.py tests/test_amap_hotel_flow.py`。记录实际通过数，再勾选任务。

## Task 2: CLI 业务结果统一为自然语言

**Files:** Create `utils/response_renderer.py`、`tests/test_cli_prose.py`、`tests/test_final_answer_boundary.py`；Modify `agents/execution_harness.py`、`cli.py`、`tests/test_sourced_cli.py`、`tests/test_workflow_cli.py`、`tests/test_juhe_cli_wiring.py`、`tests/test_ddgs_guide.py`、`tests/test_amap_hotel_flow.py`（显示测试通过真实最终结果函数建立完整 final_answer；将内部英文状态断言改成等价中文行为断言）、`README.md`。

**Interfaces:** `build_final_answer(result: dict) -> str` 返回完整自然语言回复，对 workflow 的 validated_plan、domain_results 与旧 itinerary 分支分别复用其现有校验接口；`finalize_business_result(result: dict) -> dict` 保留其他字段，只更新 final_answer，接在 `ExecutionHarness.run_turn()` 返回边界。`TripEvidenceCLI._display_results(result_data: dict)` 只打印 final_answer，保持 `markup=False`。旧单域显示助手也先建立完整返回再展示。

- [x] 先写失败测试：JSON/字典型 final_answer、工作流日程、旧 itinerary、火车报价和未知库存、酒店地点参考、攻略事实、天气、服务错误和缺失条件均输出自然语言；输出保留校验事实与来源，不泄漏内部字段，不修改输入。
- [x] 执行 `../../.venv/Scripts/python.exe -m pytest -q tests/test_cli_prose.py`，确认当前 CLI 直接打印结构化内容导致测试失败。
- [x] 实现确定性呈现函数，保留安全的自然语言正文，按现有校验结果补充必要事实与限制，缺失条件/状态用中文描述。
- [x] 将 CLI 的业务显示入口委托给呈现函数；直接回复不重复附加内容。删除或委托不再需要的 JSON 显示方法，保留用户主动请求的诊断命令。
- [x] 更新现有测试中的输出预期（状态中文化），仍检验报价、来源、旧行程的未校验事实被过滤；README 更新回复示例。
- [x] 执行 `../../.venv/Scripts/python.exe -m pytest -q tests/test_cli_prose.py tests/test_sourced_cli.py tests/test_workflow_cli.py tests/test_juhe_cli_wiring.py tests/test_amap_hotel_flow.py`，记录实际结果。
- [x] 用户澄清后的职责调整：先验证失败测试，真实 Harness 返回的 final_answer 需含全部事实和限制，CLI 不根据其他字段追加，直接模型回复及 JSON 结构保留。
- [x] 将最终组装移至 Harness 返回边界，更新显示测试的输入契约及 README，并运行定向和全量回归。

## Final Verification and Record

- [x] 执行项目 CI 指定的 `../../.venv/Scripts/python.exe -m pytest -q tests`，检查完整输出，报告失败和警告；不以定向测试替代全量测试。
- [x] 执行 `git diff --check` 和修改 Python 文件的语法检查；若环境支持，运行现有 lint 的适用检查。
- [x] 人工对照本计划的约束、两项用户要求和实际测试证据完成代码自审；若用户选多代理，则按选定模式追加独立评审。
- [x] 补充一次无需外网/LLM 的终端输出演示，覆盖注册保存和结构化结果转自然语言。
- [x] 更新任务状态与证据；未初始化 OpenSpec，不声称 verify/archive 已运行。不擅自合并、推送或创建 PR。

## Execution Record

- 2026-10-10：v0 位于现有独立 checkout，分支 `codex/information-acquisition-agent`，修改前工作区干净。父项目另有未提交变更，不触碰。
- 2026-10-10：基线定向测试 38 passed，1 项第三方 dashscope 弃用警告。覆盖 memory_flow/context、sourced_cli/workflow_cli、juhe wiring、amap flow。
- 执行方式：用户选择单代理，当前代理连续实施与整体自审；未派子代理。
- Ruling: 当前 v0 未初始化 OpenSpec，直接按本次已确认要求实施；保留此计划的 Execution Record 作为逐任务执行台账，不执行未启用的 OpenSpec 工作流。
- Task 1 RED: 新增六项注册测试实际全部失败，五项缺少 ensure_home_location，一项真实启动缺少持久化值，符合预期。
- Task 1: complete，注册与记忆/初始化定向测试 40 passed；城市持久化、用户来源保护与临时起点不覆盖均验证。
- Task 2 RED: CLI 呈现测试 11 failed、2 passed，失败均为结构化输出、缺少中文字段或事实说明；正常 direct 回复及网页链接测试已是现有行为。
- Ruling: 用户只是询问酒店API失效时的中断效果，并明确不要额外新增该场景；未增加酒店API失效/中断测试。原计划中的通用错误呈现及已有供应商测试保留。

- Task 2: complete。首轮自然语言相关定向测试 40 passed；全量测试发现天气重复显示（537 passed、1 failed），定位到 Harness 已生成的 grounded_answer 被再次呈现，移除重复摘要但保留偏好/记忆正文后，定向 21 passed。
- Task 2 自审修复：先用失败测试复现内嵌嵌套 JSON 泄漏和普通 Markdown 链接标签丢失，再改为完整 JSON 解析，定向验证通过。不增加模型调用。
- Final Verification: `../../.venv/Scripts/python.exe -m pytest -q tests --tb=short` 实际 539 passed、3 skipped、1 warning（已有第三方 dashscope 弃用警告），exit=0。
- Final Checks: 新增模块及测试 `ruff check` 通过；修改 Python 文件 `compileall -q` 通过；`git diff --check` 通过（仅 Git 已有 LF/CRLF 转换提示）。
- 人工整体自审：逐项核对本计划约束、测试与真实代码；数据持久化/身份隔离、用户来源优先、城市与起点区分、事实校验/报价保护、输出转换和兼容协议均有对应证据；没有未解决的重要发现。按用户选择未派独立审查子代理。
- CLI 演示：临时身份保存“上海”，重启读取仍为“上海”；结构化 final_answer 转成“你的长期居住城市是上海。本次准备从哪里出发？”；内部结果仍为 dict，final_answer 字段保留。演示与自动测试未调用真实外部供应商或 LLM。
- 对用户关于返回协议的说明：模型及 Harness 的 JSON 接口未改，保留 final_answer；当前 CLI 根据已校验结构化事实补充自然语言，不仅打印模型总结。工作流/价格等原有基于可信数据重建 final_answer 的行为保留。
- OpenSpec 状态：未初始化，没有运行 propose/apply/verify/archive；按用户要求没有擅自创建。
- 工作保留在 `codex/information-acquisition-agent` 的当前 checkout；本次未提交、推送或合并。
- 用户进一步澄清：完整事实本就应包含在 final_answer，CLI 只展示字段。已同步本计划职责与接口；任务2及最终验证重新进行，不重做已完成的注册任务，不额外新增酒店API失效场景。

- 用户澄清后 Task 2 RED：真实 Harness 的 final_answer 缺车次/路线日期，CLI擅自补充其他字段；三项边界测试实际全部失败，原因符合预期。
- 用户澄清后 Task 2 GREEN：完整自然语言回复在 `ExecutionHarness.run_turn()` 返回边界写入 final_answer，CLI `_display_results()` 只打印该字段。其余 JSON 字段保留；直接模型回答的文字保持原样，不增加模型调用。定向 85 passed。
- 最终重新验证：`../../.venv/Scripts/python.exe -m pytest -q tests --tb=short` → 542 passed、3 skipped、1 warning，exit=0，29.92s。新增模块/测试 lint、修改文件语法检查和 `git diff --check` 通过。
- 最终职责自审：车次、报价、日期、来源、限制说明已进入 final_answer；测试同时确认 CLI 对其他字段不做追加、原数据不被改写、直接模型答复不增加调用、金融事实仍从可信数据重建。无未解决的重要发现。
- 最终演示重新运行时发现独立启动循环导入：renderer 顶层导入 itinerary_module，经 agents.__init__ 导入 Harness 又返回 renderer，测试集的导入顺序没有触发。演示尚未通过，正在补独立进程测试并修复；任务1仍为完成状态。

- 独立启动问题 RED：新增两个子进程测试，实际均因循环导入 ImportError 失败；修正 Windows 日志读取编码后确认失败原因。
- 独立启动问题 GREEN：移除 renderer 顶层 agents 依赖，仅旧 itinerary 分支延迟导入事实校验器，避免改变业务行为。两个独立进程入口与 final/显示回归定向 26 passed。
- 最终独立进程演示通过：JSON 内的 final_answer 为完整文字，CLI 屏幕内容严格等于该字段；其他结构化字段保留，无额外模型或外部接口调用。
- 最终全量验证（覆盖独立启动修复）：544 passed、3 skipped、1 warning（原有第三方 dashscope 弃用警告），exit=0，33.59s；ruff、compileall、diff check 均通过。
- 最终状态：两项修改与用户澄清后的 final_answer 职责调整均完成，整体自审没有未解决的重要问题；当前分支保留所有修改，未提交/推送/合并。
- 分支收尾：用户明确要求合并到 main 并提交 GitHub。提交前重新运行全量测试，实际 544 passed、3 skipped、1 warning，exit=0，38.87s；远程同步后确认 origin/main 已包含先前重构且基线树与本地 HEAD 一致。本次修改将提交并合入已同步的 main，合并后复测再推送。
