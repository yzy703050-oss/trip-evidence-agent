# 用户隔离、可选会话恢复与自然回复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for single-agent execution. Use superpowers:subagent-driven-development only if the user selects multiple agents. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 修复 Windows 用户目录大小写导致的数据串用；启动直接新建对话，历史恢复通过主题列表主动选择；业务回复去掉来源元数据并减少重复。

**Architecture:** 用统一的用户存储解析器替代各模块直接拼接用户名目录，对身份、归属和旧数据兼容做集中处理。复用 SessionStore 与 MemoryManager 恢复对话，切换时重建用户会话绑定的运行组件。完整自然回复继续在 Harness 返回边界生成，CLI 只打印 final_answer。

**Tech Stack:** Python 3.11、pathlib/hashlib、Rich、AgentScope、pytest/pytest-asyncio；不增加依赖或额外模型调用。

**Spec:** 本次聊天中用户提出不同用户隔离、不要回复数据来源、恢复入口可选且显示会话主题；2026-10-11 用户以“符合”确认上一条三项设计。项目未初始化 OpenSpec，不创建其目录，不声称执行 propose/apply/verify/archive。本文展开已确认设计，不另定义产品规格；参考现有 `docs/design/v0-memory-redesign.md`、`docs/superpowers/plans/2026-10-10-residence-and-cli-prose.md` 和 `docs/superpowers/specs/2026-10-11-unified-travel-history-design.md`，本次用户要求覆盖旧计划中“公开回复保留来源及查询时间”的约束。

## Global Constraints

- 本地 user_id 按原始大小写区分；CLI 去除输入首尾空白，不做 lower/casefold 身份合并。Yzy 与 yzy 是不同身份。此隔离属于本地存储分区，不增加账号认证系统。
- 存储键采用 `user_` 加原始 user_id UTF-8 的完整 SHA-256 十六进制摘要。画像、会话、工作流、trip.md、缓存与锁均使用同一解析后的用户目录。
- 新目录持久化 identity.json 的 user_id 和版本，读取时精确校验身份。新事件、运行记录和会话状态携带 user_id/session_id，读入时校验；不能只校验恢复列表而遗漏存储层。
- 原存储路径与旧文件原样保留。仅导入能用显式 user_id 确认归属的数据；旧单文件的根 user_id 可以证明其中内嵌数据的归属。用户目录的 profile.json 可证明画像自身归属，不能证明其他无归属会话的内容。归属缺失、冲突或存在混用迹象的数据不自动恢复，不擅自拆分或分配。
- 兼容导入可重试、幂等，完整写入后才记录成功；不得覆盖较新的新目录数据。不更改真实 data/memory 来做演示，迁移测试使用临时副本。
- 启动仅询问用户身份和必要的首次居住城市，直接创建新 session_id；就绪信息不显示长会话 ID。不弹出恢复选择，不把旧任务默认当成当前任务。
- 用户输入 resume 后，显示自己的可恢复对话，按编号、主题、最近时间选择。回车取消；无历史、无效编号、读取失败均留在原对话。内部 session_id 继续使用原 UUID，不重新编号存储文件。
- 主题取最早一条 scope=session 的非空用户消息，合并空白并截断到 36 字符，超长用省略号；最近时间取最新有效会话消息时间，显示北京时间。列表最多显示最近 20 项，只用于展示，不调用模型生成标题。
- 恢复成功加载事件、摘要和本会话关联工作流，显示主题与最近一条用户输入；不重放查询或重新调用供应商。清空标记继续生效。
- 保留同用户长期偏好和用户主动跨会话查历史/继续旅行的能力；普通新规划不能自动沿用旧会话起点、日期、候选方案或活动断点。
- 回复保留已校验车次、车站、出发/到达日期和时刻、价格单位、可用情况与必要限制；ISO 时间显示为“2026年10月18日 21:15”，跨日到达保留完整日期。
- 来源名称、来源链接、查询时间不进入默认 final_answer；原始 source/fetched_at 等保留在内部结果中。网页结果默认显示标题和相关摘要，用户明确要链接时仍可直接回答链接请求，不全局删除任意正文 URL。
- 酒店地点参考不能说成已核实报价或已预订；未知房价/空房、旧报价需复查、部分失败、费用不完整仍明确表达。合并同义费用提醒，不用重复字段句堆叠同一事实。
- 不改变 MainAgent 的 JSON 协议、查询及报价校验、不增加生成回复的模型调用；CLI 不另读结构化数据追加正文。

## Review Focus

1. Windows 的 Yzy/yzy 以及恢复同名 session_id，不能共享偏好、摘要或旅行任务；Linux 同样按精确身份隔离。
2. 缺少归属的旧会话不可自动宣称属于某用户；已确认归属的旧单文件迁移失败后可重试，原始数据保持不变。
3. 会话切换后 MeteredModel 的写入回调、LazyAgentRegistry、Harness 与 Agent 缓存都绑定新会话，取消或失败则维持旧绑定。
4. 新对话遇到相同城市也不能把旧起点当成当前起点；用户明确继续旧旅行时仍可定位，多个候选先澄清。
5. 隐藏来源不能靠删除整句误删价格或攻略事实；跨日列车、未知费用及过期库存仍有准确完整的表达。

## Task 1：统一身份存储和旧数据归属校验

**Files:** Create `context/user_storage.py`、`tests/test_user_storage_isolation.py`；Modify `context/session_store.py`、`context/long_term_memory.py`、`context/workflow_store.py`、`context/trip_memory.py`、`context/memory_manager.py`；适配 `tests/test_v0_session_store.py`、`tests/test_unified_travel_history.py` 中新存储位置断言，旧迁移输入仍保留旧位置。

**Interfaces:** `user_storage_key(user_id: str) -> str`；`resolve_user_directory(root: str | Path, user_id: str) -> Path`，返回已验证身份目录并按上述策略处理明确归属的旧数据。SessionStore 增加 user_dir；LongTermMemory.user_dir、WorkflowStore.directory、TripMemory.directory 统一从解析器取得。解析器若复用 session_store 的原子写入/校验函数，采用函数内导入避免循环依赖。跨会话摘要读取 user_dir/sessions，不重新拼接 user_id。

- [x] 先写失败测试：MemoryManager(Yzy,s1) 写入偏好、消息、状态和工作流后，MemoryManager(yzy,s1) 全部不可见；两个路径的规范化大小写也不同。注入其他身份的 identity.json、事件或状态必须拒绝读取。加入路径穿越、中文身份及重复初始化场景。
- [x] 运行 `.venv/Scripts/python.exe -m pytest -q tests/test_user_storage_isolation.py --basetemp=.test-tmp/isolation --tb=short`；预期 Windows 读到另一身份内容，或缺少新的解析/校验接口。确认失败不是测试依赖问题。
- [x] 最小实现统一路径与身份元数据。跨用户会话摘要、工作流查询、旅行摘要和历史迁移全部使用同一目录；写入路径前验证归属。不要改变已有 CAS、原子写入、消息序号或截断日志修复语义。
- [x] 增加兼容测试：旧根 JSON user_id 精确匹配时迁入画像及内嵌会话；大小写不匹配不导入。已有 profile 仅导入画像，无归属的旧会话留在原位置且不列出。模拟写入中断后重试无重复，新目录更新不被旧备份覆盖；读取冲突记录不向原目录追加数据。
- [x] 运行 `.venv/Scripts/python.exe -m pytest -q tests/test_user_storage_isolation.py tests/test_v0_session_store.py tests/test_v0_cross_session.py tests/test_workflow_store.py tests/test_trip_memory.py tests/test_unified_travel_history.py --basetemp=.test-tmp/storage --tb=short`。预期全部通过；仅调整旧断言的存储位置，不删除其行为断言。
- [x] 更新执行记录。完成条件：精确身份隔离覆盖所有持久化入口，旧数据保留且归属不明数据不自动恢复，失败迁移可重试。

## Task 2：默认新对话与按主题主动恢复

**Files:** Modify `cli.py`、`context/session_store.py`、`context/memory_manager.py`、`agents/execution_harness.py`、`agents/main_agent.py`、`README.md`；Create `tests/test_cli_session_resume.py`；Update `tests/test_v0_memory_flow.py`、`tests/test_cli_onboarding.py`、初始化依赖替身及受影响的工作流上下文测试。

**Interfaces:** 保留 `TripEvidenceCLI.choose_session_id(storage_path='data/memory') -> str`，改为无提示生成 UUID。新增 `SessionStore.list_sessions(root, user_id, limit=20) -> list[dict]`（每项含内部 session_id、title、updated_at、last_user_message），复用归属校验。新增 `TripEvidenceCLI.resume_session() -> bool`；抽取 `_bind_session(session_id: str) -> None` 供初始化和恢复共用。先构造所有新组件，全部成功后整体替换原绑定。

**Context boundaries:** `get_active_workflows()` 仅返回当前会话记录的工作流，不再在空列表时回退到用户全部活动旅行。新会话的普通请求不注入过去的会话摘要与无关已知工作流；保留显式历史查询入口。Harness 对明确引用历史的请求开放同用户已知工作流，MainAgent 说明历史条件只能在用户明确继续/修改目标时复用；不明确目标时追问。当前会话中已关联任务的短回答继续正常补充断点。

- [x] 写失败测试：已有会话时启动也不提示选择、不显示 UUID；resume 列表显示主题、时间、编号且只含当前用户；回车取消、非法编号重试、空列表返回；不会将用户输入的编号当成文件路径。
- [x] 写恢复集成失败测试：从 s2 切到 s1 恢复消息/摘要/断点，模型调用数和供应商调用数保持零；恢复后新消息和计量记录写 s1。代理缓存与 Harness 使用新的 MemoryManager。读取失败保留原 session_id、对象绑定和原消息，不产生部分切换。
- [x] 写会话边界失败测试：s1 曾确认上海起点，s2 输入“我要去上海玩然后再去北京”时不收到旧出发地/断点，应按现有起点缺失流程澄清；恢复 s1 则可以继续已有断点。显式“继续上次的行程”可查同用户历史，两个目标不明确时询问目标，其他用户记录不可见。
- [x] 运行 `.venv/Scripts/python.exe -m pytest -q tests/test_cli_session_resume.py --basetemp=.test-tmp/resume-red --tb=short`；预期失败集中在启动提示、缺少恢复入口及新会话旧任务回退。
- [x] 实现默认新建、列表与编号恢复、组件重新绑定、工作流与摘要上下文边界。帮助中增加 resume；就绪文案显示“已开始新对话，可输入 resume 查看历史”。恢复只读取已保存内容，不运行旧查询。
- [x] 运行 `.venv/Scripts/python.exe -m pytest -q tests/test_cli_session_resume.py tests/test_cli_onboarding.py tests/test_v0_memory_flow.py tests/test_v0_memory_context.py tests/test_v0_cross_session.py tests/test_workflow_store.py tests/test_juhe_cli_wiring.py tests/test_amap_hotel_flow.py --basetemp=.test-tmp/resume --tb=short`。
- [x] README 更新启动与恢复示例，说明身份大小写、旧数据保留策略。更新执行记录。完成条件：恢复是主动命令，用户看懂会话内容，取消/失败不影响当前对话，恢复后读写绑定正确且新任务无旧起点污染。

## Task 3：自然呈现经过校验的事实

**Files:** Modify `utils/response_renderer.py`、`agents/main_agent.py`、`tests/test_cli_prose.py`、`tests/test_final_answer_boundary.py`、受来源/日期文案影响的 `tests/test_sourced_cli.py`、`tests/test_workflow_cli.py`、`tests/test_juhe_cli_wiring.py`、`tests/test_amap_hotel_flow.py`、`tests/test_ddgs_guide.py`；Create `tests/test_natural_reply_regressions.py`；Update `README.md`。

**Interfaces:** 保持 `build_final_answer(result: dict) -> str`、`finalize_business_result(result: dict) -> dict` 与 CLI 显示契约。新增内部 `_format_date(value: str) -> str`、`_format_datetime(value: str) -> str`，有效输入转换为中文日期及北京时间时刻；不可解析值保留可读原文，不编造日期。按已校验 domain/validated_plan 输出完整事实，替代来源拼接与重复 schedule/_offer 描述。来源隐藏在业务组装处完成，不全局正则删 URL。

- [x] 用日志中的两段列车和酒店地点建立纯离线 fixture。失败断言：final_answer 不含来源名/来源链接/查询 ISO 时间；D6、D9、车站、338/317 元及 655 元合计保留；跨日到达保留完整日期；酒店未知价格和库存仍说明。断言行程时刻只出现一次，费用不完整提醒只出现一次。
- [x] 增加其他分支失败测试：单域火车、酒店报价/地点、天气、攻略、网页、旧 itinerary、错误/部分结果、过期数据；已有自然 direct 回复仍原样；明确要网址的 direct 回复保留网址。finalize 不修改原结果，内部 source/fetched_at 和原始时间仍存在，CLI 屏幕正文等于 final_answer，模型调用数不增加。
- [x] 运行 `.venv/Scripts/python.exe -m pytest -q tests/test_natural_reply_regressions.py --basetemp=.test-tmp/reply-red --tb=short`；预期因当前来源拼接、ISO 时间或重复描述失败。
- [x] 改进确定性模板与时间格式，按行程顺序串联交通和住宿，费用集中表达；保留所有现有事实校验和可用性边界。MainAgent 的面向用户生成指引同步隐藏来源，不改变内部协议。调整旧公开来源断言为“正文无来源、内部仍有来源”，不删除真实性验证。
- [x] 运行 `.venv/Scripts/python.exe -m pytest -q tests/test_natural_reply_regressions.py tests/test_cli_prose.py tests/test_final_answer_boundary.py tests/test_sourced_cli.py tests/test_workflow_cli.py tests/test_juhe_cli_wiring.py tests/test_amap_hotel_flow.py tests/test_ddgs_guide.py --basetemp=.test-tmp/replies --tb=short`。
- [x] 更新 README 回复示例和执行记录。完成条件：各公开回复分支符合新要求，未核实事项不被隐藏，完整最终回复仍由 Harness 负责，不新增调用。

## Final Verification and Record

- [x] 逐任务核对实现、失败测试、通过结果与上述完成条件；不能仅凭勾选框报告完成。
- [x] 运行 `.venv/Scripts/python.exe -m pytest -q tests --basetemp=.test-tmp/full --tb=short`，读取完整输出；覆盖 Windows 大小写复现及现有工作流/历史兼容。记录实际测试数、跳过及警告。
- [x] 执行 `git diff --check`、修改 Python 文件的 compileall；运行新增模块的适用 ruff 检查。检查独立子进程 `from cli import TripEvidenceCLI` 和 renderer 导入，避免循环依赖。
- [x] 整体自审：用户隔离、读写路径、迁移中断、绑定替换、明确恢复、未知费用与事实来源内部保留均有测试证据。若选多子代理，为每项独立任务安排规格与代码质量评审，并修复后复审；单代理则由当前代理整体自审，不自动派子代理。
- [x] 用临时数据做离线演示：两种大小写身份各有偏好和对话；启动直接输入需求；resume 显示标题并恢复；两段交通自然回复不含来源和查询时间。演示不改真实历史、不调用外部供应商。
- [x] 人工逐项核对用户已确认设计与本计划。项目未启用 OpenSpec verify/archive，明确报告未运行；不创建伪 change 或归档记录。
- [x] 完成后向用户呈现保留工作分支、合并或 PR 的适用收尾选择；未获得本次授权前不推送、合并或创建 PR。

## Follow-up：常住城市默认起点与待补充回复

用户反馈 yzy12 首次填写重庆后，输入“我明天想去上海玩”仍追问起点。最新要求覆盖旧居住城市计划的“背景信息不作为起点”约束：新旅行首段缺起点时使用当前用户已保存的 home_location，明确起点优先，后段仍从上一目的地出发；常住城市等于目的地时不生成同城火车路线，仍澄清。本次默认不写回画像，不借用历史行程。单代理继续执行，不增加模型调用。

- [x] 失败测试：真实 CLI/Harness 从持久偏好补重庆首段，重启/新会话仍可用；明确北京起点优先，后段不覆盖，画像不变，无偏好仍只追问起点。
- [x] 实现：workflow_contracts.create_workflow 最小补默认起点并保留 preference 来源；同步 MainAgent 提示词、修复提示词、CLI 背景摘要及运行时 plan-trip 指南。更新 README 默认行为。新旧更新接口更改默认起点后改标来源 user。
- [x] 失败测试及实现：needs_input 的公开回复仅保留断点问题，不展示空路线、预算或内部草稿校验错误；内部校验记录保留。
- [x] 恢复提示：新用户没有历史时说明可直接聊天、以后可恢复；重新构造 MemoryManager 验证持久对话能被列出，不泄露其他用户对话。
- [x] 定向及全量测试、代码自审、规格人工核对，更新本计划与验证记录。此前完成证据不作为本次新增修复的通过证据。

## Execution Record

- 2026-10-11：用户确认三项设计；用户已选择单代理；当前代理实施并整体自审，不派子代理。
- 排查证据：data/memory/Yzy/profile.json 的 user_id 为 Yzy；Windows 下 data/memory/yzy 解析到同目录。临时 SessionStore(Yzy,s1) 写入消息，SessionStore(yzy,s1) 能读取，确认隔离漏洞。
- 指定恢复会话包含此前“我明天要去北京”“上海”等输入；最新两段工作流为上海→北京→上海。已确认旧上下文存在，但未将不调用真实模型的检查描述为完整复现模型误判。
- 基线：会话存储、跨会话、启动和回复四组测试 30 passed、1 warning（既有 dashscope 弃用警告），exit=0。pytest 退出清理既有系统临时目录时有 WinError 5，因此后续测试使用项目内专用 basetemp。
- 此计划已按用户选择的单代理方式实施，执行证据见后续记录及verification文件。

- 执行分支：codex/user-session-isolation；用户未请求新工作树，使用当前checkout的工作分支。创建分支需sandbox escalation，自动审批通过；未修改main分支历史。
- 全量基线：首次新basetemp父目录缺失导致132个setup错误；创建父目录后重新执行，561 passed、3 skipped、1 warning。该失败属于测试环境准备，非产品回归。
- Task 1 RED：8 failed，均为预期隔离/归属缺失；GREEN：存储、迁移、工作流及跨会话定向49 passed。自审新增三项失败测试，分别复现清空后重导入、损坏无归属日志阻断启动、另一大小写身份误删旧文件，随后全部修复。
- Task 2 RED：7 failed、1 passed；恢复入口与旧活动任务回退按预期失败，启动测试因任务1已换路径而暂时通过，随后补入旧目录fixture覆盖原启动提示路径。恢复取消/失败原绑定不变，成功后事件与计量写入目标会话；真实Harness新规划上下文不带旧起点。
- Task 3 RED：来源/时间/重复描述5项按预期失败；旧报价测试起初使用不被domain guard接受的needs_revalidation字段，改用真实工作流重建结果fixture后确认保留原有未知/过期提醒。公开来源断言改为内部保留来源、正文隐藏。
- Ruling：跨会话仅回答“重庆”不能自动恢复旧起点断点。旧测试改为恢复原session后回答，仍检查同workflow/task、日期及停留方案不变；明确“上海这段换全季”属于指向既有路段，历史查询入口保留。未关联到当前会话的直接WorkflowStore写入不应成为当前活动任务，对应测试显式关联。
- Ruling：初始化与恢复共用_bind_session；增加可选的raw_model/providers/memory参数以复用启动已准备组件，整个绑定成功后才替换当前对象。取消/读取失败不替换。
- 兼容记录：不确定归属的旧会话/旅行保留原文件，不自动导入。精确归属的旧单文件、画像及有owner的分拆记录可导入；identity.json记录已导入会话，防止清空后复活。
- 首轮全量回归：578 passed、3 skipped、6 failed，原因是旧公开来源预期、旧活动回退假设和回复关键词；逐项修正后全量589 passed、3 skipped、1 warning，32.05s。
- 自审修正了清理来源助手时引入的缩进错误；该次测试收集失败已明确定位，不算GREEN证据。初始化替身要求原有命名参数，保留该调用契约后复测通过。

- 最终自审RED/GREEN：非对象identity/event/state/run四项在修复前均因AttributeError失败，修复后拒绝为ValueError；回复中的重复城市路线补失败断言后修复，不删除具体车站信息。
- 最终全量验证：593 passed、3 skipped、1 warning，31.67s，exit=0；ruff、compileall、git diff --check通过。无需真实接口/模型的终端演示通过，显示主题/时间/编号，不展示session_id，价格与未知费用完整且无来源元数据。
- 最终代码自审按单代理要求执行，没有未解决的重要发现；逐项人工核对已确认设计。OpenSpec未初始化，verify/archive未执行；详见docs/superpowers/verification/2026-10-11-user-session-and-replies.md。
- 分支收尾：改动保留在codex/user-session-isolation工作区，未合并、推送或创建PR；合并/PR作为用户可选后续动作呈现，不自动执行。
- Follow-up RED：4 failed、6 passed，分别复现常住城市未补起点、CLI返回needs_input、追问夹带空预算/内部错误及空恢复提示缺说明。GREEN 定向68 passed。自审补两项来源变更RED，修复新旧更新接口后均通过。
- Follow-up最终验证：`.venv/Scripts/python.exe -m pytest -q tests --basetemp=.test-tmp/followup-home-final --tb=short`，605 passed、3 skipped、1既有警告，34.78s，exit=0；ruff、compileall及git diff --check通过。默认起点来自当前用户的可信偏好，明确起点/后段起点/现有任务不被覆盖；Renderer不改变内部断点与校验。新增CLI集成使用离线模型和供应商，日期提取由模型替身提供，未声称真实模型与票务联调通过。
