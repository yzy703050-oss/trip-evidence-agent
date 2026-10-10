# 默认关闭模型推理模式实施计划

> **For agentic workers:** 使用 superpowers:executing-plans。延续用户选择的当前分支单代理执行与整体自审。

**Goal:** 按用户“推理模式应该是要关闭的”要求，将实际运行与测评的DeepSeek调用默认设为disabled，并重测复杂旅行。

**Architecture:** 在config统一构建模型生成参数，CLI、测评、健康检查及知识库初始化入口复用；所有共享模型的Agent和压缩调用继承。旅行完成仍由既有程序校验约束，提示词明确非阻塞缺口不阻止completed，不自动把模型partial改为completed。

**Tech Stack:** Python、AgentScope、pytest、当前DeepSeek接口。

**Spec:** 用户本轮明确配置要求；沿用[规划补全正式设计](../specs/2026-10-10-planning-preflight-design.md)的P15/P17/P19/P22及酒店地点推荐边界。项目未初始化OpenSpec，不创建其结构；人工核对。

## 范围与完成条件

- 不支持DeepSeek选项的其他模型不发送thinking字段；测评显式low/high对照仍允许，省略选项与生产配置一致。
- 日期建议、模拟来源和酒店报价未知在没有硬报价要求时是披露事项；阻塞缺口、预算未核实及陈旧证据仍不得completed。
- 真实模型配显式模拟旅行接口，覆盖复杂三段、新建部分方案、查询、局部修改和暂停；保留耗时与失败证据。
- 当前分支保留，不push/merge，不更换旅行Provider，不修改RAG检索或embedding实现。

## Task 1: 统一生成参数与完成指南

**Files:** config.py、cli.py、utils/llm_resilience.py、evals/run_preflight_simulated.py、evals/main_agent/live_runner.py、知识库初始化模型入口、agents/main_agent.py、plan-trip指南、tests/test_deepseek_model_config.py。

**Interface:** get_model_generate_kwargs(config=None, *, thinking=None)返回独立生成参数；DeepSeek默认disabled，其他模型无专属字段。

- [x] RED：测试CLI实际参数包含disabled；测试其他模型兼容、显式对照参数与返回值隔离。
- [x] GREEN：实现公共参数函数并接入全部运行入口；明确已有成功校验后finish completed，不反复校验同一版本。
- [x] 回归：模型配置、完成校验及规划测试；完整pytest与git diff --check。

## Task 2: 实测、文档与提交

- [x] 使用默认配置跑真实模型+模拟接口用例，记录分阶段延迟及通过情况；失败时根据原始记录诊断，不改写结果。
- [x] 更新README与新增测评报告；原85秒/41秒对照保留为历史，不混算新默认。
- [x] 整体自审、规格核对、本地提交并保留当前分支。

## 完成证据

用户本轮明确要求默认关闭推理，沿用当前任务的单代理执行选择。默认参数RED4项失败；配置及规划回归36通过。真实模型首次测评8/11，原始记录保留；追溯提前结束、derived硬报价要求后，补充RED3/GREEN55。恢复已有查询保护RED1/GREEN9。最终完整测试429通过、3跳过、1条既有告警，18.08秒；最终真实模型11/11、P01–P33证据33/33。

技术复核补充到已确认正式设计：首次可查询领域不得因个人缺项跳过；恢复不强制重查；需要酒店不能推导出必须报价。符合原P02/P15/P29，无新增活动或估价要求。最终整体自审检查了Provider专属参数兼容、显式测评覆盖、预算、硬要求与完成校验；本地提交并保留分支，未push/merge。测评详见[报告](../../evals/2026-10-10-thinking-off-default-evaluation.md)。
