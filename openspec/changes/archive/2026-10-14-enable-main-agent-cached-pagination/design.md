## Context

动机见 proposal.md。正式行为见 specs/ 下两个 delta。

- WorkflowRunner._batch_context 仅检查必需领域是否有非空候选；满足时只开放 commit_batch，循环也拦截 schedule_queries。
- ToolExecutor 已支持 candidate_offset；CandidateStore 同查询命中缓存，candidate_view 完成既有过滤、排序及切片，不需新接口。
- 批量调用读取 private.query_records 归并。ToolExecutor.execute 当前在写入 row.view_offset 之前复制 query_records，翻页偏移可能在批量路径丢失。
- project_domain_result 为避免二次切片而传 offset=0，但同时重写输出 view_offset 和 has_more；需将切片起点与展示页偏移分开。
- execute_query_batch 在执行前按外部查询预算预留，即使随后命中缓存也先受预算门槛影响。
- 工作区已有其他未提交修改，尤其 travel_prompts.py、context_projection.py；实施时在现有内容上小范围修改并保留这些工作。

## Goals / Non-Goals

**Goals:** 复用既有 schedule_queries/query_plan 与候选缓存，开放模型自主选择后页；程序严格证明请求是本地读取；默认成功路径仍一次选择提交。

**Non-Goals:** 不开发接口分页，不自动刷新或放宽条件，不改排序，不实施 filter-candidates-by-hard-constraints 或 RAG 变更，不引入信息子 Agent。

## Decisions

### 1. 开放带边界的查询动作

没有问题且全部领域有候选时，若当前有效 train/hotel 视图确有后页，则同时开放 commit_batch、schedule_queries。新增明确的本地翻页范围信息；主提示说明仅可取后页，找到合适组合即可提交，无须遍历所有页。没有后页仍保留仅提交路径。存在真实问题时保留既有受控修复语义。

不简单地全面删除 commit_required：那会允许模型把缓存浏览扩展成重新请求供应商。

### 2. 执行前验证本地缓存覆盖

在 Runner 绑定查询后、execute_query_batch 执行前验证该受限模式的请求。目标必须是当前任务/查询版本的 train/hotel，查询身份应可唯一匹配既有有效记录，供应商参数及约束保持一致，refresh 不得为 true；candidate_offset 必须等于当前页偏移加实际条数且仍有下一页。禁止同批重复目标。

规范化参数后的缓存键必须已命中有效结果，禁止缓存缺失时退回供应商请求。缓存集合在单轮执行中共享，并在工具读取边界保留 cache-only 内部保障；不将这一内部控制变成可由模型伪造的供应商参数。

### 3. 保留分页身份与前页证据

先更新查询记录的 view_offset 再复制进 query_records，保证批量归并保存最新页。视图层对已经切片的 items 不再按全局偏移切一次，但输出保留真实 view_offset，并根据总数与实际页内容计算 has_more。完整 items、query_id、result_revision、来源及 fetched_at 不因翻页变化，前页引用仍可用于最终方案。

模型上下文仍只含每次有界候选；不累积所有已浏览候选到每轮上下文。

缓存读取失败仅记录本次读取失败，不将原查询容器标成失败或清空其当前引用。预算或无进展停止时，以程序生成的 candidate_browsing_incomplete 状态交给 utils/response_renderer.py 附加未浏览范围说明，避免统一回复渲染覆盖该说明。保留原有 draft_task、validate_workflow、finish 的兼容处理；本地翻页限制只拦截越界查询、修复或询问动作，不改变非法动作的错误分类。

实施时在 result_views 的 limits 中增加内部 already_paged 标记，将 items 的切片起点与输出 view_offset 分开；query_views 显式传递实际窗口。新增 agents/cached_pagination.py 集中生成可用后页命令和校验批量本地读取，避免在较大的 workflow_runner.py 中展开全部校验。该模块消费当前有效视图、查询记录和 CandidateStore，不改变候选排序。受限路径在工作流副本上绑定命令，整批校验通过后才执行，避免非法请求经 bind_query_plan 的日期补全改写工作流。

### 4. 分开处理本地读取与外部预算

仅对已验证的 cache-only 请求绕过外部请求预算预留，不消耗外部请求次数；仍受原决策次数、总时间和上下文预算限制。普通查询不获得这个豁免。无后页、非法偏移或无缓存时返回具体动作反馈，不重新查接口；重复动作沿用 no_progress 结束保护。

### 5. 与既有规格协调

consolidated-travel-execution 中“已有候选先提交”的要求修改为允许受控缓存翻页。接口缺字段和无报价时禁止反复补查的规则保留；翻页不得被用于宣称能补出接口从未提供的字段。正常候选已合适时仍为原来的两次主模型路径；确需后页时按需追加选择调用。

## Risks / Trade-offs

- 翻页增加主模型调用和延迟 → 只在模型判断需要时读取，保留原决策上限，不强制遍历。
- 误将翻页当新查询 → Runner 和工具读取边界均确认缓存覆盖，测试断言供应商请求数不变。
- 偏移丢失造成重复第一页 → 覆盖批量归并、连续多页及模型投影的回归。
- 后页未读完但决策预算耗尽 → 如实说明已查范围和仍未查看候选，不把未发现组合说成客观无解。
- 后续硬过滤变更改变候选集合 → 本次维持现有过滤、排序，未来在固定条件及版本下统一计算分页。

## Migration Plan

不迁移业务存储，沿用 view_offset、query_id 和 result_revision；旧记录缺偏移按 0。回滚动作开放和 cache-only 路径即可恢复当前行为，候选证据继续可读。

## Review Status

用户于 2026-10-14 回复“开始修改”，确认前述范围与设计。已重新对照主流程、工具执行、批量归并、候选视图、预算、已有测试及正式规格完成技术复核；补充了查询副本绑定与分页校验模块的实现边界，不改变已确认行为。

详细 TDD 计划：docs/superpowers/plans/2026-10-14-enable-main-agent-cached-pagination.md。依用户提供的 AGENTS.md，在计划交付后由用户选择单代理或多子代理执行。当前尚未修改产品代码。
