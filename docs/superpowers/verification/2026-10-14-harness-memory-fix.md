# 实施自审与规格核对

单代理整体自审，依据统一实施计划和fix-harness-amendments-and-compaction/add-runtime-file-logging两份正式change。实施前脏基线保存在该计划专用ledger工作区，没有重置现有改动。详见[真实验收](../../evals/2026-10-14-harness-memory-fix/README.md)。

| 要求 | 实现与证据 |
|---|---|
| 只读/修改互斥 | contracts.validate_plan；routing6例+意图/只读回归 |
| 本地修改证据闭环 | WorkflowRunner候选窗口、目标筛选、跨页引用、版本/授权/重复页校验；selection12例+amendment/pagination回归 |
| 原记录与模型历史分离 | project_history_messages；重启、管理字段、摘要输入、本轮请求4例；原事件不改写 |
| 全请求压缩闭环 | request_fits使用实际build_messages/tools/已有response；增量摘要最多3次、输入余量、失败/并发保护、输出复用、精确80%触发及15147旧边界11例 |
| 配置保护 | 默认/example/本地20000，trigger16000，显式覆盖；temperature0.3/回退0.3/max1116/thinking disabled保持 |
| 普通日志 | 真实UTF8、上海日期、轮转/幂等/路径切换、CLI入口/导入、模型/数据库失败、工具异常、压缩值11例 |
| 真实行为 | 连续12/12；额外2类缓存/20k边界；按实际保存方案和答案共同核对 |

全量最终pytest退出0：938 passed、3 skipped，66.46秒。首次全量926通过后继续审查发现初始补丁绕过、旧请求位置和普通替换过度澄清，均写RED测试再修复；后续全量出现旧条件修改恢复回归1failed/936passed，修复后上述最终全量通过。新增文件Ruff退出0；修改产品文件与实施前基线逐条比较新增0发现，广域仍203项原有问题。

重要发现已处理：初始带引用补丁必须二阶段；候选充分时不以澄清代替任务；本轮请求置于历史后且明确背景语义；摘要max_tokens按余量收紧；恰好软阈值也触发；人数等条件不能被字面酒店解析器误当目标；工具抛异常也记录名称并传播；无候选的旧程序调用者保留恢复流程。

延后项：没有模型专用tokenizer，ASCII场景估算可偏小（实测已记录）；原有203项广域Ruff风格问题未清理。当前turn内阶段压缩继续未来规划，不是漏做。

执行了openspec instructions verify，spec-driven返回不存在verify artifact。人工核对全部需求/场景/设计/任务/证据，未将严格validate当成verify。正式归档在验收后执行；未合并、未推送、未统一提交其他用户改动。

## 归档与收尾

openspec archive已成功同步agent-context-isolation、saved-trip-amendment、token-session-compaction和新增runtime-file-logging规格。两个归档目录为2026-10-14-fix-harness-amendments-and-compaction及2026-10-14-add-runtime-file-logging。用户选择保留当前分支与工作区，未提交/合并/推送。

实施判断：沿用当前非main开发工作区，以保留已有脏基线；代价是本次与之前改动同目录，整合时须按记录区分。使用Windows原生工具替代Bash记录脚本，避免环境不兼容；代价是账本维护需人工核对。按用户新要求纳入80%软阈值并先更新正式文档；代价是触发频率提高，增加摘要成本，已限制最多3次并记录用量。全部判断可追溯至用户选择或环境。
