# 既有行为与重构验收对照

本次为结构重构，不新增、删除或修改产品行为规格。正式行为继续由 openspec/specs/ 管理；.openspec.yaml 设置 skip_specs: true，归档不制造新的产品需求。

| 正式规格 | 本次必须保留的行为与验收 |
| --- | --- |
| consolidated-travel-execution | ExecutionHarness 转交唯一编排流程；纯记忆操作、独立查询、新规划、修改、冲突修复及恢复保持行为；普通成功回放不增加模型/供应商调用 |
| bounded-travel-tool-results | 每页 5 条、现有排序、完整证据、本地翻页不请求供应商、偏移及前页引用保持 |
| project-model-runtime | 不依赖 AgentScope；config_agentscope 兼容模块仍可导入，模型适配及用量语义保持 |
| transactional-agent-storage | 用户隔离、SQLite、旧数据读取和迁移入口保持；本次不迁移业务数据 |
| saved-trip-read-only / saved-trip-amendment | 已有行程读取、局部修改、版本验证及未授权组件保留 |
| agent-context-isolation / progressive-context-disclosure | 不因为代码迁移引入重复工具结果、完整缓存或其他 Agent 历史 |

验收以现有功能测试、入口导入/猴补丁兼容回归、评测脚本输出与退出码、旧报告可定位、迁移文件完整性共同证明。不能用测试收集不到旧测试、删除兼容测试或改写既有业务断言达到通过。

