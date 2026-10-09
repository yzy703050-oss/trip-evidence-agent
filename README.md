# 行程有据

行程有据是一个基于 AgentScope 的命令行多 Agent 差旅助手。用户用自然语言提出行程、政策或历史偏好问题；主 Agent 分派任务，harness 按依赖执行所需子 Agent，再按完整性选择直返或综合。当前 V0 包含可恢复的会话记忆和 EDD（评测驱动开发）基线。

本仓库只包含 **V0 命令行版本**。火车、酒店与攻略已建立来源契约和输出保护，火车查询可接入已授权的聚合数据接口；未查询到的数据保持未知。

## 它怎样工作

MainAgent 做每轮决策，harness 按依赖执行任务。可调度子 Agent 只有
preference、memory_query、rag_knowledge、information_query。
阶段 1 提取偏好、查询记忆与制度；阶段 2 信息获取读取本轮新偏好和前序结果；
需要综合或生成行程时，由主 Agent 在阶段 3 完成。行程规划不再独立调度。

信息获取负责条件整理、模型原生工具调用、结果检查、补查和总结。
它的内部工具是 train_search、hotel_search、travel_guide、weather_query、web_search，
工具自身不调用 LLM。主 Agent 只看到四个子 Agent 的能力，查询工具 schema 只交给信息获取。

完整简单查询走「决策 → 信息获取 → 程序直返」，主 Agent 最终阶段模型调用为 0。
缺项、部分成功、跨 Agent 答案和行程请求进入综合。统一结果包含 status、
finalization_method、final_answer、results、domain_results、travel_conditions、
missing_fields，行程请求还包含 itinerary。

火车和酒店默认最多展示 5 个候选，本轮全量池与缓存继续保留。
查看后续窗口不向聚合接口发送分页参数。每次信息获取最多 6 次模型调用、10 次工具调用，
单工具超时 30 秒。主 Agent 最多反馈一次，复用同一缓存；信息获取最多执行两次，
最终综合最多调用两次。不能静默修改已确认硬条件。实际查询发出后，后续模型或记录失败
不会重放整轮请求。

RAG 内部、模型与 embedding 配置及外部 RAG 项目保持不变。
酒店和攻略 Provider 仍待接入，当前返回 unavailable。

## 会话与记忆

启动时输入 `user_id`；可选择已有 `session_id` 恢复会话，或回车新建。两者分别标识用户与会话；`turn_id`、事件序号和 `tool_call_id` 只用于内部关联。

```text
data/memory/{user_id}/
├─ profile.json                 # 可查看、可手动修改的用户偏好
├─ trips.json                   # 历史行程
└─ sessions/{session_id}/
   ├─ events.jsonl             # 按发生顺序追加的会话事件
   ├─ state.json               # 压缩摘要与覆盖边界
   └─ runs.jsonl               # 调度、耗时、调用量和 token 记录
```

`events.jsonl` 可记录用户消息、助手回复，以及信息获取模型调用工具时的调用和结果。当前 V0 的子 Agent 调用属于**调度执行阶段**，不会伪装成模型工具调用。会话变长时，程序只在已完成轮次或阶段的安全边界选择压缩范围；不会切在工具调用与结果之间。摘要供模型读取，原始事件仍保留。跨会话查询使用用户画像及相关历史摘要。

用户可在 CLI 中执行 `preferences` 查看画像，执行 `preferences set hotel_brands '["全季"]'` 修改偏好，或用 `preferences delete hotel_brands` 删除。手动设置优先于 Agent 自动提取。

## 本地运行 V0

需要 Python 3.11，以及支持 OpenAI 兼容接口的模型服务。在**仓库根目录**执行；Skill 注册器使用相对路径发现子 Agent。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

在本地 `.env` 至少配置 `LLM_API_KEY`、`LLM_BASE_URL` 和 `LLM_MODEL`。CLI 会调用真实模型。不要提交 `.env`。

```powershell
python cli.py
```

按提示输入用户 ID，随后输入自然语言问题。输入 `help` 可查看 `status`、`history`、`preferences`、`health` 和 `exit` 等命令。单独检查模型连通性可运行 `python cli.py health`。

### 启用 V0 知识库问答

虚构企业的演示政策文档位于 `.claude/skills/ask-question/data/documents/`，不代表真实企业规定。V0 使用本地 `BAAI/bge-small-zh-v1.5` 嵌入模型和 Milvus Lite；模型文件与生成的向量库不在 Git 中。将模型放在 `data/models/bge-small-zh-v1.5/`，或让首次运行按配置下载。准备好资源后，在仓库根目录初始化：

```powershell
python .claude/skills/ask-question/script/init_knowledge_base.py
```

初始化脚本会重建目标 collection，请勿将它用于需要保留原有内容的知识库。Windows 上若项目路径含中文，Faiss 可能无法在该路径写索引；可在初始化和运行 CLI **之前**设置纯英文目录，例如 `$env:V0_RAG_DB_DIR = 'C:\trip-evidence-rag'`。该变量只改变向量数据库位置。

## 测试与真实测评

不调用模型的回归测试：

```powershell
python -m pytest -q tests
```

退休角色的旧在线演示已由主 Agent 和内部工具离线回归替代。离线测试结果以本仓库当前测试运行输出为准。

真实 EDD 会检查计划与实际 Agent 调用是否一致、执行是否成功、答案关键事实、RAG 来源与依据、跨会话回忆、行程约束，并记录延迟、模型调用量及 token。先用 CLI 完成相应会话，再用对应 case 对已保存的日志评分：

```powershell
python -m evals.v0_memory.runner --cases evals/v0_memory/cases.json --user-id USER_ID --session-id SESSION_ID
```

`evals/v0_memory/` 另有 `trip_case.json` 和 `memory_case.json`。它们分别检查行程与跨会话偏好；评测器读取既有会话日志，不会替你重新发起模型请求。

### 2026-10-08 真实测评（来源保护之前的历史记录）

| 场景 | 结果 | 模型调用 | 耗时 |
| --- | --- | ---: | ---: |
| 北京住宿政策问答 | 通过：找到政策依据，回答 500 元/晚 | 2 次 | 12.80 秒 |
| 上海至北京三日行程 | 预算内有估算组合（最低 2520 元）；**严格上限保证未通过**，最贵组合为 3080 元 | 4 次 | 43.29 秒 |
| 跨会话酒店偏好 | 通过：从用户画像恢复“全季” | 3 次 | 10.69 秒 |

三例合计 9 次模型调用、12,215 输入 token 和 13,852 输出 token。记录的成本字段目前为未知：模型返回的用量不足以还原实际账单中的缓存命中和时段价格。详细过程、修复及评测边界见 [V0 真实测评报告](docs/evals/2026-10-08-v0-live-evaluation.md)。一条 RAG 用例不足以代表整体检索准确率；行程中的车次、价格等也未接入实时核验。

## 代码入口

| 位置 | 职责 |
| --- | --- |
| `cli.py` | 命令行入口、用户交互与一轮请求 |
| `agents/main_agent.py` | 任务决策与按需综合/行程生成 |
| `agents/execution_harness.py` | 依赖执行、缓存、直返、反馈与保存 |
| `agents/lazy_agent_registry.py` | 发现、实例化和缓存 Skill 子 Agent |
| `.claude/skills/*/script/agent.py` | 各专业子 Agent 的 Python 实现 |
| `context/` | 会话事件、用户画像、压缩与遥测 |
| `evals/v0_memory/` | V0 EDD 用例与评分器 |

记忆方案见 [设计说明](docs/design/v0-memory-redesign.md)。本仓库尚未附带开源许可证。

## 有来源的出行数据

火车、酒店与攻略查询采用独立 Provider 接口；火车可配置聚合数据授权查询，酒店和攻略数据源待接入；未配置授权服务时返回 unavailable，不生成演示报价。候选项展示来源、查询时间及缺失条件，失败不表示无票或无房。行程模型仅选择查询结果中的 ID 并建议活动地点和时间；价格、车次、房型、天气、开放和预约事实由程序从查询结果重建。费用显示已知小计与未报价类别，旧 estimated_budget 不构成预算保证。当前其他费用尚未接入，因此完整预算保持未核实。

行程地点通过已查询的地点引用展示；缺少来源时仅显示地点待核实的通用活动安排。模型的自由地点或城市散文不会进入回复。规划超时、服务异常和无效返回使用固定失败信息，编排与评测均记录失败。

### 启用聚合数据火车查询

从已开通[火车订票查询接口](https://www.juhe.cn/docs/api/id/817)的聚合数据后台取得 KEY，在本地 `.env` 中填写 `JUHE_TRAIN_API_KEY`。示例文件仅保留空值，真实 KEY 不得提交到 Git 或复制进日志。配置后重启 CLI；KEY 为空时火车查询返回 `unavailable`。

当前仅调用列车站到站查询接口，不支持下单或订票 MCP。每次查询可能计费，请核对账号套餐和额度。查询仅支持北京时间今天起的 15 个日历日（今天至第 14 天）；超出范围不发送请求。起终点按用户确认的站名原样查询，不保证城市名覆盖全市车站；空结果请核对具体车站。

结果展示车次、席别、价格、可订状态、聚合数据来源和带时区的查询时间。只有明确可预订且余票为正或明确“有”的席别可用于已知费用小计；未知库存保持未知。接口失败或无权限会明确显示不可用或错误，不表示无票。自动测试只使用假 KEY 和本地响应，真实接口联调须单独执行。
