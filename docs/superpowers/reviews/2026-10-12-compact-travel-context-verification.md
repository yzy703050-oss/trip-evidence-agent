# 上下文精简、摘要定位与跨段修复验证记录

日期：2026-10-12。分支：codex/compact-travel-context。用户选择单代理实施及整体自审。

## 正式依据

实施遵循 progressive-agent-context-disclosure 的 proposal/design/tasks 和四份 specs；详细步骤见 ../plans/2026-10-12-compact-travel-context.md。归档后正式需求同步到 openspec/specs，变更文档移到 archive 的同名目录。

## TDD 与业务证据

| 任务 | 失败依据 | 验证依据 |
|---|---|---|
| 领域精简/去重/子隔离 | 新用例初始 5 failures；额外子数据泄漏及 date_options 重复均先失败 | compact_context + isolation/runner/reply 首轮 GREEN 49 passed；去重相关 24 passed |
| 独立小结索引 | 初始 4 failures，定位/保存均发现全量读取；结构性损坏 {} 用例先失败 | index/trip_memory/matching/unified_history GREEN 40 passed；损坏后的二次定位不加载快照 |
| 旧 task 修复 | 合约不接受 repair_tasks/模块缺失，初始 4 failures | 合约/固定条件/组件/依赖/Runner 首轮 GREEN 56 passed；checkpoint 跨会话恢复与价格进展、错误引用保护均有回归 |
| 成功资源与调度 | 初始 4 failures，缺校准模式/成功计数/实际调度 | 计数/子循环/Runner 等 GREEN 36 passed；额外 telemetry 5 passed |

直接验证的关键场景：定位不读未选中的大型 JSON；保存一个行程不读其他完整快照；真实 MainAgent 请求无候选网址/原始证据；未知抵达日期和 hotel_place 含义保留；完整存储不因投影变化；子调用不带其他任务的数据；旧段可受控重开并保留有效酒店；无关/过期/任意条件写入被拒绝；调度停止后恢复同一修复 checkpoint，不重放外部查询；新价格证据不误判成无进展；实际模型与调度分开计数，失败不消耗成功额度。

## 资源校准

执行 `D:/python_3.11.9/python.exe scripts/calibrate_resources.py`，全部功能完成后采样七条成功路径：单目的地默认返程、三段、五段、小结匹配复用、恢复修改人数、同 turn 中段修复、非 workflow 下一页/缓存反馈。使用生产 MainAgent/Runner/子 Agent 与明确的离线 SDK 响应和数据替身，隔离临时用户存储，真实供应商请求为零。

| 配置作用域 | 最大值 | 最终默认值 |
|---|---:|---:|
| 同次上下文预算 | 7427 | 14854 |
| 完整响应输出估算 | 558 | 1116 |
| 主模型 / turn | 14 | 28 |
| 主模型 / 当前 task | 6 | 12 |
| 子模型 / 一次委派 | 2 | 4 |
| 实际调度 / turn | 12 | 24 |
| 辅助委派 / task（含重开） | 2 | 4 |
| 非 workflow 委派 / run | 2 | 4 |

完整来源、场景、映射和独立边界见 ../../evals/2026-10-12-resource-calibration.json。token 为 utf8_conservative 估算，不是实际 provider usage。没有真实模型/供应商硬窗口验证，不能从离线路径推断所有未来行程都足够。未静默裁剪两倍值。输出从旧配置 8192 改为离线两倍观测值 1116，这一额度后续应随更复杂真实成功样本重新校准。

熔断、工具次数、超时、候选页大小和反馈业务协议不随两倍值变化。旧 main_steps_per_task/main_extra_steps 只保留显式调度上限缺失时的构造兼容。

## 单代理整体自审

- 核对主入口、嵌套 source、子结果和 preflight，移除候选网址、重复 date_options 和重复条件；保留比较所需的固定日期来源、人数及预算约束。
- 排除了 TripMemory → list_history 的隐藏全量读取；公开历史统计接口仍可读取其完整历史，正常发现/渲染用索引。
- 索引使用用户隔离、文件锁、原子写和目标版本核对；迁移/外部改动可重建，竞争变化不得给旧摘要盖上新文件时间戳。
- 修复不写入任意硬条件，程序控制根任务、引用绑定与依赖队列。无进展签名包含事实变化而排除纯版本变动，使用 hash 避免把完整签名重复存入上下文。
- 成功资源按响应完成后统计，流式取消不冒充成功；主预算贯穿初始化/匹配/摘要/回复，调度累计与子委派累计分别保留。
- 修复了回归中的轻量 Memory 测试替身兼容；旧 saved_plan 全量暴露断言按新的摘要行为调整；固定六次循环测试改为显式六次额度，避免把默认值当成业务协议。

## 最终检查

最终全量测试：690 passed、3 skipped（可选/live 场景）、1 条 Dashscope SDK 弃用警告；使用最终配置与短 TEMP 路径。必要静态检查使用 Ruff F 正确性规则、compileall 和 git diff --check。宽泛 E/I/UP/B 风格扫描发现既有压缩代码风格债务，不声称全仓库全部 lint 规则通过。

本安装 schema 只有 proposal/specs/design/tasks，没有 verify 工作流。运行 OpenSpec validate 只证明文档结构；实现一致性采用人工逐项核对，不能把它称作 OpenSpec verify。按四份规格核对了需求、场景、代码和上述实际测试证据。

用户测试豁免保留：不新增或手工触发单 turn 超预算停止及提示旅程分段专项测试。供应商失败排查/自动补查和模拟策略改造延期。本次不修改真实接口调用或熔断策略。

归档已执行：`openspec archive progressive-agent-context-disclosure --yes --json`，归档目录 `openspec/changes/archive/2026-10-12-progressive-agent-context-disclosure`，四项能力共 16 条新增正式需求同步成功。最终 pytest 重跑 690 passed / 3 skipped，Ruff F、compileall、git diff --check 均通过。保留当前工作分支，未推送或创建新的 PR。
