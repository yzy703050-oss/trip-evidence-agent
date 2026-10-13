# 真实模型契约与API模拟验收自审

执行方式：用户指定单代理、当前目录；未派评审子代理。正式依据为fix-live-model-contracts的两份spec、design和tasks；实施计划为同名2026-10-13计划。

## 任务证据

| OpenSpec任务 | 实现与证据 |
| --- | --- |
| 1.1 | 初始8次/13,451 token失败记录保留，统一正式change和实施计划；技术复核按实际程序结构执行 |
| 1.2 | model_contracts提案schema/sample供initialize/repair共用；原非法提案仍拒绝、单程预算保持；对应失败测试先RED后GREEN |
| 1.3 | policy_preflight对象适用范围、来源/单位/金额/缺口严格检查；Runner有限错误审计；真实RAG引用和旧制度/未审批例外测量 |
| 1.4 | 实际tool schemas、批量参数契约、金额语义、报价单位、过滤计数、新模型金额类型、allowed_actions及显式partial停止；复杂冲突4调用、两段火车不重放 |
| 1.5 | evals/api_shaped_simulation以HTTP注入复用Juhe/Amap生产解析；响应多席别/余票/跨夜、坏行/重复POI/坏坐标；来源明确模拟，高德不虚构报价；raw/parsed记录已保存 |
| 1.6 | 无name的到达日期query_plan先因普通工具绑定测试失败，再依据prepare_task.query_mode绑定arrival工具；显式name及参数仍走原严格检查；主提示移除重复基本查询说明 |
| 1.7 | 确定只有酒店报价能力缺口时，在完整guard和保存后程序停止partial；遗漏模型stop标记先RED；混合未知火车时间不短路；定向真实模型/RAG重测3调用10,991 token |
| 2.1 | 全量801 passed/3 skipped、必要静态检查、全部测量cost与逐次输出；详见下文 |
| 2.2 | CLI无verify命令；人工逐条场景核对完成，strict validate通过，随后同步归档 |

## 实际验证

- 原始响应/绑定TDD：4 failed（3模块缺失断言、1错误train_search绑定）；最小实现后相关103 passed。
- 初始提示旧可选query_plan说明与新契约矛盾：1 failed，移除后相关61 passed。
- 缺报价遗漏停止：1 failed（第二次选择模型被调用）；修复后能力/政策/Runner相关55 passed，其中混合未知火车时间保持原修复。
- 最终全量：`.venv/Scripts/python.exe -m pytest -q --basetemp .superpowers/test-tmp/api-shaped-final`，**801 passed, 3 skipped in 45.01s**，日志`.superpowers/sdd/2026-10-12-consolidate-travel-model-calls/api-shaped-final-tests.log`。
- 新文件及本次契约测试Ruff通过；全仓F821/F822/F823/F811通过；`git diff --check`通过。既有travel_data/tools.py仍有4项E501/E731风格问题，记录于changed-legacy-ruff.log；没有宣称全仓所有Ruff规则零问题。
- `openspec validate fix-live-model-contracts --strict --no-interactive`通过；当前CLI没有verify，不声称运行OpenSpec verify。
- 测试临时目录首次指定时父目录缺失产生28个setup errors，创建父目录后重跑61 passed；此前系统temp的pytest退出清理出现权限警告，最终验证使用workspace内独立basetemp，无该警告。

## 规格与失败边界核对

提案结构样例走create_workflow，预算、purpose和关键词类型保持；旧非法字段仍拒绝。新模型金额只接受有限非负JSON数字，历史Decimal字符串保持读取及数值等价比较，固定值不被刷新覆盖。

RAG先检索虚构当前500元/晚制度、废止900元和需审批800元例外，实际政策提取均一次；来源来自真实本地检索。未知强制规则和政策检索失败仍保留阻塞，没有通过删约束来完成旅行。

报价夹具第一次700/760元每晚被过滤，主模型一次定向酒店refresh，新版本380/420元满足500元；火车两段各一次。原始金额和时间证据保留，派生每晚金额仅展示，guard独立检验。

普通旅游2调用/7,515 token完成，普通出差3调用/10,871 token完成，复杂冲突4调用/16,652 token完成。纯高德无房价任务仍partial，3调用/10,991 token按程序能力判断停止；这项验收通过不表示行程完成。三种可完成场景最后的真实测量发生在能力缺口补丁之前，补丁之后全量及报价冲突回归通过；没有将它们说成补丁后又重跑了真实模型。

本轮实际70,478 token含高德5调用/24,449 token失败和定向重测；四个最终验收记录46,029 token。十轮全部保存测量累计293,956 token/79调用，所有失败原样保留，不能用成功子集代替累计调试费用。报告脚本只读历史，不调用模型。

## 模拟与实际能力

供应商未联网，只替换http_post/http_get。价格、余票、跨夜和POI清洗复用生产代码。模拟数据检验接口形状与处理链路，车次、地理坐标与门店均是测试数据，不证明真实供应商的实时准确性。

高德业务cost不作为房价；普通出差/冲突的酒店报价为单独HotelOffer契约夹具，当前高德不具备该能力。未增加生产故障自动假数据兜底，没有改变实际API故障策略。

当前分支codex/readme-agent-diagrams和原有未提交工作保留；没有提交、推送、合并或清理其他工作。

实际归档完成：2026-10-13-fix-live-model-contracts，OpenSpec同步新增5项需求，源change移入archive。当前codex/readme-agent-diagrams分支保留，无提交、推送、合并。
