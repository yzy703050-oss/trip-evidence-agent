# 三场景延迟测试脚本

依据用户已确认的简短方案：仅测火车价格查询、单站规划、多站规划，复用真实模型与显式模拟供应商的现有测评入口；VPN由用户手动控制。项目未初始化OpenSpec，本次不创建OpenSpec结构。当前分支单代理执行。

- [x] 先写离线脚本测试：注入测试用命令输出，不调用网络，验证恰好三个case、显式关闭推理、任意工作目录启动、输出摘要、失败返回非零以及独立输出目录。执行测试确认脚本缺失导致失败。
- [x] 新建scripts/test_latency.ps1：自动选择项目Python，调用evals.run_preflight_simulated的三个现有case，保存日志并显示简明表；summary.csv、summary.md、完整原始记录保留。标签默认no-vpn，只表示用户标签，不检测或修改VPN/代理。
- [x] 运行上述测试和现有延迟报表测试，检查PowerShell语法；README补充关闭VPN后的运行命令及vpn标签比较方式。完成自审，不在无法确认VPN状态时冒充无VPN实测。

验证：`../../.venv/Scripts/python.exe -m pytest tests/test_latency_smoke_script.py tests/test_latency_report.py -q`。验收为用户关闭VPN后一次命令即可得到三个场景耗时；工具为模拟供应商，数据说明不得隐藏。

实施记录：离线测试先因脚本缺失失败；实现后发现Windows PowerShell 5.1的JSON数组输出与PowerShell 7不同，最小复现确认后改为直接赋值，并展平执行参数。最终10项测试通过，含三行精确耗时、未调用阶段零值、重复执行独立目录及失败保留结果。README已补充运行和比较方法。自审确认未修改VPN/代理、业务代码或已有测评入口，且测评记忆隔离在独立输出目录。未发起本次真实模型测评，避免将未核实的网络状态标成无VPN数据。
