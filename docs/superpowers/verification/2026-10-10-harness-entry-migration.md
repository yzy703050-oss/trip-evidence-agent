# Harness 入口迁移验证记录

## 范围与依据

用户要求移除前文讨论的旧 OrchestrationAgent 兼容包装，统一直接使用 ExecutionHarness。项目未初始化 OpenSpec，本次按已确认的入口迁移方案进行单代理实施和人工核对，未运行 OpenSpec verify/archive。

## 已完成任务

- [x] CLI 持有 app.harness，直接实例化 ExecutionHarness 并调用 run_turn。
- [x] agents 包导出 ExecutionHarness，删除 orchestration_agent.py 和 normalize_schedule 别名；调度规则仍使用原 business_schedule。
- [x] 独立规划入口、真实模型评测脚本和既有测试全部迁移；需要空注册器的测试显式传 agent_registry={}。
- [x] README 更新当前入口与旧名称状态；历史设计和评测文档保持历史记录。

本次没有改动 ExecutionHarness、WorkflowRunner、模型提示词、工具实现及记忆存储协议。外部调用方如果使用旧导入或 app.orchestrator，需要迁移到新入口；不保留旧名称兼容层。

## 验证证据

1. 先将 test_forward_records_one_final_message 改为通过 app.harness 注入真实 ExecutionHarness。旧 CLI 仍读取空 orchestrator，出现预期失败：AttributeError，不能执行 run_turn。
2. 迁移产品入口后，受影响测试 77 passed。
3. 全量：python -m pytest -q tests，517 passed、3 skipped、1 条现有 DashScope 弃用警告，26.19 秒，退出码 0；提交前再次运行同一完整测试命令，517 passed、3 skipped，38.83 秒，退出码 0。
4. 公共导入 from agents import ExecutionHarness 与直接模块导入一致，CLI 构造正常。
5. 运行代码和测试中不再有 OrchestrationAgent、orchestration_agent、normalize_schedule、.orchestrator 引用；差异格式检查通过。
6. 展示入口使用 Python UTF-8 模式执行 print_banner/print_help。额外的 cli.py --help 尝试进入了交互启动，并遇到现有 GBK 终端不能输出横幅 emoji 的编码问题；该 CLI 未提供 --help 参数，本次不修改终端编码或新增命令参数。

## 整体自审

初始化依赖、懒加载注册器、普通查询转交、付费查询不重放、偏好保存、会话记录与独立入口均由迁移后的既有回归测试覆盖。任务范围仅是入口去包装和调用方命名迁移，没有新增第二套调度引擎。
