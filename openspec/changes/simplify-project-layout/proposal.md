## Why

用户要求整理代码与目录，同时保留现有功能和兼容入口。当前 agents/ 包含 30 个文件并混合模型角色、行程编排及领域规则；docs/ 的 458 个文件中有 394 个属于评测产物，根目录另有大量临时日志，增加阅读和维护负担。

## What Changes

- 将行程编排相关模块集中到 workflow/，让 agents/ 主要表达模型、协议和现有兼容入口；统一项目内部导入。
- 保留 python cli.py、现有 CLI 命令、MainAgent、ExecutionHarness、WorkflowRunner 及旧配置入口，保持插件加载和数据路径。
- 将历史评测与诊断归入 docs/archive/；当前评测产生的 JSON、日志、SQLite 等默认写到已有 data/evals/，文档保留人工可读结论和索引。
- 将根目录已有 *.log 非破坏地归入 data/logs/history/，补充项目结构和运行文档。
- 保留测试覆盖、历史证据和旧功能；不执行数据库迁移，不修改模型行为、查询排序、缓存翻页、预算或政策规则。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

无产品行为变更。本 change 是结构重构及开发产物路径整理，使用 skip_specs: true；verification-scope.md 引用现有正式规格及回归场景，不人为新增一套产品需求。

## Impact

主要影响 agents/ 下的 10 个 workflow_* 模块、条件/修改/分页辅助模块及其导入方，evals/ 和 scripts/ 的产物路径，少量依赖历史响应文件的测试，README、文档索引和 .gitignore。保留 context/、travel_data/、utils/、tests/、.claude/skills 的主体结构，避免一次变更遍及所有包。

工作区包含此前未提交的实现与其他修改，实施时基于当前工作区保存迁移清单，不能以回滚、删除或整体提交覆盖这些工作。两个已有未实施 OpenSpec change 保持独立，不在本次顺带实现。

## Scope Confirmation

2026-10-14：用户选择“整理代码与目录，保留现有功能和兼容入口”。本提案的具体目录映射和兼容清单待用户审阅；尚未移动、删除或修改运行代码。

